# Audio Truncation Fix — Investigation & Resolution

**Date:** 2026-04-30 → 2026-05-01
**Branch:** `akg-dev`
**Final commit:** `65a3bdd1` — `fix: replace ffmpeg with parec | lame for audio capture`
**Earlier (partial) fix:** `16b96272` — `fix: rotate ffmpeg output every N seconds` (moved cap from 7.5 min → 10 min, did not eliminate it)

> **TL;DR** — ffmpeg's audio-side libavformat input has a ~10 min cap that **cannot** be defeated from outside ffmpeg, even by rotating output via the segment muxer. Replacing the audio capture path entirely with `parec | lame` (no ffmpeg in the audio path) fixes it. The video path (`audio_only=false`) still uses ffmpeg and is unaffected.

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

### 6. Segment muxer (partial fix — moved cap, didn't eliminate it)
Theory: per-output-context cap. Each new ffmpeg output via `-f segment` should reset libavformat/libmp3lame internal state.

```python
ffmpeg_cmd = [
    "ffmpeg", "-y", "-thread_queue_size", "4096",
    "-f", "alsa", "-i", "default",
    "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "1",
    "-f", "segment", "-segment_time", "240",
    "-segment_format", "mp3", "-reset_timestamps", "1",
    f"{base}_part_%04d{ext}",
]
```

Plus bytewise concat in `_concatenate_segments()` after ffmpeg exits.

**Result:** moved the cap from 7m 27s → ~10 min. **Three independent runs (bots 113, 114, 115) all stopped at exactly ~13.9 MB / 9m 38s of audio.** Bot 115 was a real morning standup that ran 53 min wall-clock — we lost ~43 min of meeting audio. Segment rotation reset the *output* state but the cap is in the **shared input/demuxer state** across the entire ffmpeg process, not per-output.

## Root cause

**ffmpeg's audio-side libavformat input pipeline hits a per-process cap somewhere around 10 min of consumed audio**, while Pulse continues to deliver samples. The cap is shared across all output segments (so segment muxer can't escape it). The exact subcomponent is unknown — likely the libavformat ALSA/Pulse demuxer's internal buffer accounting or thread state.

Properties:
- Per-process — multiple output rotations don't reset it
- Triggered by total audio time consumed, not wall clock or file size
- Probabilistic — same code captured 44 min on bot 101 once
- Impossible to defeat from outside ffmpeg (parec on the same Pulse source keeps delivering at 92 KB/s while ffmpeg's mp3 freezes)

## The actual fix — replace ffmpeg with `parec | lame`

`bots/bot_controller/screen_and_audio_recorder.py` audio-only path now bypasses ffmpeg entirely:

```python
parec_cmd = [
    "parec", "--device=auto_null.monitor", "--raw",
    "--rate=44100", "--channels=1", "--format=s16le",
]
lame_cmd = [
    "lame", "-r", "--bitwidth", "16", "--signed", "--little-endian",
    "-s", "44.1", "-m", "m", "-b", "192", "--quiet",
    "-", self.file_location,
]
self._parec_proc = subprocess.Popen(parec_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
self._lame_proc = subprocess.Popen(lame_cmd, stdin=self._parec_proc.stdout,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
self._parec_proc.stdout.close()  # so SIGPIPE flows correctly on shutdown
```

- **parec** is the diagnostic that proved Pulse delivers audio indefinitely. Single-purpose Pulse client, ~6 MB RSS, no buffering state of its own.
- **lame** is a 25-year-old standalone mp3 encoder reading raw PCM from stdin. No libavformat. No demuxer state to corrupt.

Shutdown ordering matters: terminate parec first → its socket close sends EOF to lame → lame writes the trailing mp3 frames + footer and exits cleanly. We give lame 10s to finalize on EOF before SIGTERM, then 5s before SIGKILL.

The video path (`audio_only=False`, mp4 with x11grab) is **unchanged** and still uses ffmpeg.

## Validation

Two consecutive long-meeting runs on `parec-lame-20260430`:

| Bot | Wall-clock | Recording duration | Final size | Result |
|---|---|---|---|---|
| 116 | ~20 min | **20 min 13 sec** | 27.8 MB | ✅ matches meeting |
| 117 | ~24 min | **23 min 42 sec** | 32.6 MB | ✅ matches meeting |

ffprobe of bot 117's mp3:
```
duration = 1422.27 sec  (23 min 42 sec — was capped at 9m 38s before)
size     = 34,136,188
bit_rate = 192,009  ✓ CBR mp3, perfect
```

AUDIO_DIAG progression for bot 117 (linear growth, no stalls):
```
t+ 480s  size= 11,517,952  growing=True   ⭐ past 7.5min ffmpeg cap
t+ 600s  size= 14,401,536  growing=True   🎯 past 10min segment-muxer cap
t+ 690s  size= 16,560,128  growing=True
t+1000s+ size> 25,000,000  growing=True
t+1410s  size= 33,845,248  growing=True   ← bot stayed past meeting end
```

Cleanup chain:
```
Auto-leaving meeting because there was no audio for 600 seconds
parec stopped
Stopped audio capture pipeline (parec | lame) → /tmp/bot_xxx-rec_yyy.mp3
Uploading recording via HTTP POST to bot-scheduler
HTTP file upload finished
Recording file saved event recorded in DB
forcing BOT_LEFT_MEETING (existing post-processing fix)
Pod exit 0
```

Bot-scheduler downstream completed normally on both runs — Gemini correction, document row, GCS audio + markdown all written.

## Operational notes

### What changed in production
- All 3 deployments (`attendee-web`, `attendee-worker`, `attendee-scheduler`) on image `parec-lame-20260430`
- `CUBER_RELEASE_VERSION` secret bumped so future bot pods inherit
- Dockerfile installs `lame` package alongside `pulseaudio-utils` (which provides `parec`)
- Configmap unchanged
- **Bot-scheduler unchanged** — zero downstream changes. It still receives one mp3 per meeting at the same endpoint.

### What stayed in place
- The `AUDIO_DIAG` daemon thread — now tracks `self.file_location` size + parec/lame process state; useful tripwire for any future regression
- All earlier fixes on this branch:
  - `46b0dfcb` — ffmpeg shutdown timeout (still relevant for video path)
  - `0309f2e1` — force `LEAVING → POST_PROCESSING` transition
  - `eea49066` — `module-suspend-on-idle` unload in entrypoint.sh (harmless and stays)
- HTTPFileUploader and GCSFileUploader unchanged
- Cleanup chain order unchanged

### Failure modes — what happens if…

| Scenario | Outcome |
|---|---|
| Pod is SIGKILLed mid-meeting by Kubernetes | The mp3 file on disk is whatever lame had finalized up to the point of kill. mp3 frames are independently decodable, so even a partial file is playable from start to where it was cut. **Worst-case audio loss: the last few hundred ms** (lame's internal frame buffer). |
| parec dies mid-meeting | lame sees EOF on stdin, finalizes the mp3 cleanly. `check_process_health` reports unhealthy and bot is force-left by the orchestration layer. Recording up to that point is preserved. |
| lame dies mid-meeting | parec writes to a closed pipe, gets SIGPIPE, exits. Recording stops; whatever lame already wrote is on disk. Same recovery as above. |
| Disk fills up | lame's write fails, lame exits. Bot pod has 10 GiB ephemeral storage = ~70 hours of mp3 — won't realistically happen for any meeting. |
| Bot-scheduler is down at upload time | HTTP upload retries 3× then falls back to native GCS uploader. Recording lands in `gs://attendee-recordings-neusis-platform/` for manual replay. |
| Cloud Run 32 MiB cap (huge recordings) | The existing GCS-notification mode in `http_file_uploader.py` handles files >30 MB. A 4-hour mp3 at 192 kbps is ~340 MB and will use that path automatically. |

### Why this change is small

The audio-only ffmpeg invocation was 18 lines. The parec | lame replacement is 25 lines. We deleted segment-muxer (45 lines), `_concatenate_segments` (28 lines), and the parec sidecar diagnostic (40 lines). **Net diff: -195 / +125** — the file is shorter and simpler than before the investigation started.

## Quick references

| File | What |
|---|---|
| `Dockerfile` | Adds `lame` to the apt-get install line for pulseaudio-utils |
| `bots/bot_controller/screen_and_audio_recorder.py` | parec | lame pipeline + `AUDIO_DIAG` tripwire + parec/lame-aware `check_process_health` |
| `entrypoint.sh` | Unload `module-suspend-on-idle` after Pulse start (kept from earlier experiment) |
| `bots/bot_controller/bot_controller.py:~690` | Force `LEAVING → POST_PROCESSING` on cleanup |
| `bots/bot_controller/http_file_uploader.py` | GCS-notification mode for files >30 MB |

## Things to look for in future regressions

If recordings ever truncate again, the `AUDIO_DIAG` lines tell you immediately where:

```
AUDIO_DIAG t+...s size=N growing=False stalled_for=...s | parec=running | lame=running
```

- **`growing=False` while parec=running and lame=running** — bug is back somehow. Check whether one of the pipe ends has stalled (lame's stderr may have errors).
- **`lame=exited_rc=N`** — lame crashed. Check stderr; could be malformed input (parec format mismatch) or disk issue.
- **`parec=exited_rc=N`** — parec crashed. Pulse server may have died or socket was severed.

The video path (`audio_only=False`, mp4 with x11grab) still uses ffmpeg and is **NOT** affected by this fix. If long-form video recordings start truncating, that's a different code path and the parec | lame approach doesn't apply directly to it (parec is audio-only). The video path would need its own segment-muxer or process-rotation strategy at that point.
