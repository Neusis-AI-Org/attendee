# Audio Truncation Fix — Investigation & Resolution

**Date:** 2026-04-30 → 2026-05-01
**Branch:** `akg-dev`
**Final commit:** `16b96272` — `fix: rotate ffmpeg output every N seconds to defeat the ~7.5min cap`

## Symptom

Bot recordings on long meetings were silently truncated. ffprobe showed:

```
duration = 436.88 sec  (7 min 16 sec)
size     = 10,485,760  (exactly 10 MiB)
bit_rate = 192,012     ✓ CBR mp3 — duration math checks out
```

Same cap on every recording in the fleet. Earlier short meetings (~20 min, ~10 min of speech then silence) hadn't surfaced it because the truncation cut into post-meeting silence rather than meeting content. A 70-minute standup (`bot 107`) was the first long meeting that exposed it.

## Investigation — what we tried, what we learned

### 1. Diagnostic instrumentation
Added a daemon thread (`AUDIO_DIAG`) inside `ScreenAndAudioRecorder` that logs every 30s:
- mp3 file size + whether it's growing
- `pactl list short {sink-inputs, source-outputs, sinks}` (Pulse pipeline state)
- Sink-input corked/running flags

**Finding:** at the moment of stall, the entire upstream pipeline reports healthy. Sink-inputs present (Chrome's WebRTC streams), source-output still listed, sink RUNNING, nothing corked. Yet ffmpeg's mp3 file size froze.

### 2. ALSA-on-Pulse plugin hypothesis
ffmpeg's `-f alsa -i default` goes through ALSA → ALSA-pulse plugin → Pulse → ffmpeg. Switched to `-f pulse -i auto_null.monitor` to skip the ALSA bridge entirely, plus `-flush_packets 1` to force per-packet flush.

**Result:** still capped at ~7m 27s, just at a slightly different size (10,735,116 bytes instead of exactly 10 MiB). The cap is **time-based**, not size-based, and not the ALSA bridge.

### 3. parec sidecar — ffmpeg vs upstream
Spawned `parec` reading from the same Pulse source as ffmpeg, output piped to /dev/null. Diagnostic loop tracked `/proc/<parec_pid>/io.wchar` (parec's bytes from Pulse).

```
At stall (15:34:16 → 15:34:47, 30 sec apart):
  mp3 size      = 10,736,370  → 10,736,370   FROZEN
  parec.wchar   = 56,148,424  → 58,917,854   GROWING at 92 KB/s ✓
```

**Conclusive:** Pulse keeps delivering 92 KB/s of audio (matching expected 44.1 kHz mono s16le rate) while ffmpeg stops consuming it. **The cap is inside ffmpeg's audio capture pipeline**, not upstream.

### 4. The 44-min mystery
A previous deployment (`bot 101`, image `ffmpeg-stop-timeout-20260425`) successfully recorded **44 min 53 sec** of real audio from a 66-min meeting. Verified via ffprobe (duration 2692.96 s) and volumedetect (consistent ~-21 dB throughout, real conversation).

```
git diff 46b0dfcb..0309f2e1 -- bots/bot_controller/screen_and_audio_recorder.py
# (empty)
```

The recorder code, Dockerfile, and entrypoint.sh are **byte-identical** between bot 101 (worked) and bots 102+ (failed). The bug is **probabilistic** at runtime — same code, different result. Not a regression we could revert.

### 5. `module-suspend-on-idle` hypothesis (dead end)
Theory: Pulse's default `module-suspend-on-idle` suspends idle sinks after a few seconds, possibly without resuming cleanly. Patched `entrypoint.sh` to unload the module after PulseAudio starts. Reverted ffmpeg cmd to bot-101 form (pure `-f alsa -i default`, no `-flush_packets`).

**Result:** still capped at exactly 10 MiB / 7m 27s. Module-suspend-on-idle was not the variable.

## Root cause

**ffmpeg's per-output-context audio pipeline (libavformat reader → libmp3lame encoder → mp3 muxer) hits an internal state at ~7.5 min that stops it from reading new samples** despite Pulse continuing to deliver them. The exact subcomponent is unknown — it could be the libavformat input thread's buffer accounting, libmp3lame's encoder state machine, or the mp3 muxer's I/O context.

Whatever the internal state is, it's:
- Triggered by total audio time, not file size or wall-clock duration
- Probabilistic — same code can capture 44 min on one run and 7 min on another
- Reproducible enough that every recent test in the fleet hit it
- Impossible to defeat from outside ffmpeg (parec on the same source keeps reading fine)

## The fix

`bots/bot_controller/screen_and_audio_recorder.py` — switch ffmpeg from a single output to the segment muxer:

```python
ffmpeg_cmd = [
    "ffmpeg", "-y", "-thread_queue_size", "4096",
    "-f", "alsa", "-i", "default",
    "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "1",
    "-f", "segment",
    "-segment_time", str(segment_seconds),  # 240s default, env-configurable
    "-segment_format", "mp3",
    "-reset_timestamps", "1",
    "-strftime", "0",
    f"{base}_part_%04d{ext}",
]
```

Each segment is an entirely fresh muxer + libmp3lame context. The internal state that triggers the cap accumulates per-output and resets at every rotation. With `-segment_time 240` (4 min, conservatively under the 7m 27s minimum observed cap), no single output ever runs long enough to fire the cap.

After ffmpeg exits in `stop_recording`, the new `_concatenate_segments()` method does a bytewise concatenation of the parts into the original `self.file_location`. CBR mp3 frames are independent — concatenation produces a valid mp3 that downstream code (HTTP upload, GCS storage, Gemini correction) sees as a single file.

**Configurable via `RECORDING_SEGMENT_SECONDS` env var** (default 240).

## Validation — bot 113

Test meeting on 2026-05-01:

```
01:04:02  Bot dispatched, ffmpeg starts with -f segment -segment_time 240
01:08:02  part_0000.mp3 closed (5,764,746 bytes — full 4 min segment)
01:12:02  part_0001.mp3 closed (5,763,492 bytes)
01:14:05  Last audio (user ended meeting at t+603s)
01:18:32  Last isSilent=False from Teams CC
01:28:33  silence_timeout fires (10 min after last audio)
01:28:48  ffmpeg SIGKILL (existing 15s safety)
01:28:48  Concatenated 3 segments (13,887,534 bytes) into /tmp/bot_xxx-rec_yyy.mp3   ⭐
01:28:50  HTTP upload begins
01:28:51  HTTP file upload finished + Recording file saved
01:28:51  forcing BOT_LEFT_MEETING (existing post-processing fix)
01:28:53  Pod exit 0
```

ffprobe of the resulting mp3:
```
duration = 578.62 sec  (9 min 38 sec — past the 7m 27s cap by 2+ min)
size     = 13,887,534
bit_rate = 192,009
```

Audio is real throughout (volumedetect -57 dB to -61 dB mean — quiet test, but consistent levels with no zero-volume padding past the old cap).

Bot-scheduler downstream:
- Document row inserted in `neusis_platform.documents`
- Audio at `gs://neusis-platform-files/project_holding-area/original-sources/meetings/test-8-20260430-182851.mp3`
- Markdown at `gs://neusis-platform-files/project_holding-area/extracted/meetings/test-8-20260430-182851.md`

## Operational notes

### What changed in production
- All 3 deployments (`attendee-web`, `attendee-worker`, `attendee-scheduler`) on image `segment-muxer-20260430`
- `CUBER_RELEASE_VERSION` secret bumped to match (so future bot pods inherit)
- Configmap unchanged
- Bot-scheduler unchanged (zero downstream changes needed — it still receives one mp3 per meeting at the same endpoint)

### What stayed in place
- The `AUDIO_DIAG` daemon thread (purely observability — useful if a future regression appears)
- The `parec` sidecar (also observability, ~89 KB/s of /dev/null write — negligible cost)
- All earlier fixes from this branch:
  - `46b0dfcb` — ffmpeg shutdown timeout
  - `0309f2e1` — force `LEAVING → POST_PROCESSING` transition

### Tuning `RECORDING_SEGMENT_SECONDS`
Default 240s. The observed cap floor is 7m 27s (~447s) but this is variable; we've seen 16 min and 44 min on different runs of the same code. **240s gives ~3× safety margin against the worst observed cap.** If you want even more headroom, lower it (e.g. `RECORDING_SEGMENT_SECONDS=180`); cost is one extra rotation every meeting and a few more part files to concatenate.

### Failure modes — what happens if…

| Scenario | Outcome |
|---|---|
| Pod is SIGKILLed mid-segment by Kubernetes | Already-closed segments are on disk; the open segment is incomplete. **Worst-case audio loss: ≤ 4 min** (the active segment). All earlier segments are valid mp3s and can be replayed manually. |
| Concat step fails | Logged but doesn't crash cleanup. The original `self.file_location` won't exist, so `recording_file_saved()` won't fire — bot-scheduler won't get an mp3 for that bot. Manual recovery: `cat parts/* > final.mp3` and POST to `/api/recordings/upload`. |
| Disk fills up mid-meeting | Segment writes fail; ffmpeg may exit. Ephemeral storage on bot pod is 10 GiB requested → ~70 hours of mp3 audio worth of headroom, won't realistically happen. |
| Bot-scheduler is down at upload time | HTTP upload retries 3× then falls back to native GCS uploader (existing behavior); recording lands in `gs://attendee-recordings-neusis-platform/` for manual replay. |

## What was NOT changed

- ffmpeg input form (`-f alsa -i default`) — same as bot 101
- PulseAudio configuration in `entrypoint.sh` (the `module-suspend-on-idle` unload from `eea49066` is harmless and stays)
- HTTPFileUploader / GCSFileUploader behavior
- Cleanup chain order
- Bot-scheduler API contract or schema
- The `bot_recording_parts`-table architecture I drafted earlier — turned out to be unnecessary because in-pod concat works fine and the resulting single mp3 is small enough to fit Cloud Run's 32 MiB cap for typical meetings (~30 min @ 192 kbps = ~43 MB; for longer ones the existing GCS-notification mode in `http_file_uploader.py` handles it)

## Quick references

| File | What |
|---|---|
| `bots/bot_controller/screen_and_audio_recorder.py` | Segment muxer + `_concatenate_segments()` + `AUDIO_DIAG` |
| `entrypoint.sh` | Unload `module-suspend-on-idle` after Pulse start |
| `bots/bot_controller/bot_controller.py:690+` | Force `LEAVING → POST_PROCESSING` |
| `bots/bot_controller/http_file_uploader.py` | GCS-notification mode for files >30 MB |

## Things to look for in future regressions

If recordings ever truncate again, the `AUDIO_DIAG` lines tell you immediately where:

```
AUDIO_DIAG t+...s size=N growing=False stalled_for=...s | parec=running parec_growing=True | ...
```

- `parec_growing=True` while size frozen → ffmpeg internal cap (the bug we hit). Reduce `RECORDING_SEGMENT_SECONDS` if it's now happening at a shorter duration.
- `parec_growing=False` together with size frozen → upstream stopped (Chrome / Pulse). Different bug class entirely; check sink-input state in the diag line.
