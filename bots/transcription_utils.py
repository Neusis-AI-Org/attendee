import io
import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Sequence

import requests

from bots.models import Credentials, Recording, TranscriptionFailureReasons, TranscriptionSettings, Utterance

logger = logging.getLogger(__name__)


def is_retryable_failure(failure_data):
    return failure_data.get("reason") in [
        TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED,
        TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED,
        TranscriptionFailureReasons.TIMED_OUT,
        TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED,
        TranscriptionFailureReasons.INTERNAL_ERROR,
    ]


def get_empty_transcript_for_utterance_group(utterances):
    # Forms a dict that maps utterance id to empty transcript
    return {utterance.id: {"transcript": "", "words": []} for utterance in utterances}


def get_mp3_for_utterance_group(
    utterances: Sequence[Utterance],
    *,
    silence_seconds: float = 3.0,
    channels: int = 1,
    sample_rate: int,
    sample_width_bytes: int = 2,  # 2 => 16-bit PCM (s16le)
    bitrate_kbps: int = 128,
    io_chunk_bytes: int = 256 * 1024,
) -> bytes:
    """
    Given an array of Utterance instances whose audio blobs are ALWAYS RAW PCM,
    returns an MP3 (as bytes) containing each utterance concatenated with `silence_seconds`
    of silence between them.

    Streaming properties:
      - PCM is streamed into ffmpeg stdin in chunks (no concatenation of PCM in memory).
      - Silence is streamed as zero-bytes in chunks.
      - MP3 is read from ffmpeg stdout in chunks.

    Important note:
      - Returning `bytes` inherently means the final MP3 is fully held in memory at the end.
        Its size is roughly (bitrate_kbps / 8) * duration_seconds (plus small overhead).

    Assumptions:
      - PCM is signed 16-bit little-endian (s16le). If yours differs (e.g. float32), change -f/-sample_width_bytes.
      - All utterances share the same sample rate (enforced), unless `sample_rate` is provided.

    Raises:
      - ValueError / RuntimeError on invalid inputs or ffmpeg failure.
    """
    if not utterances:
        raise ValueError("No utterances provided.")

    target_sr = sample_rate

    bytes_per_second = target_sr * int(channels) * int(sample_width_bytes)
    total_silence_bytes = int(round(float(silence_seconds) * bytes_per_second))

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        # input: raw pcm from stdin
        "-f",
        "s16le",
        "-ar",
        str(target_sr),
        "-ac",
        str(int(channels)),
        "-i",
        "pipe:0",
        # output: mp3 to stdout
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{int(bitrate_kbps)}k",
        "-f",
        "mp3",
        "pipe:1",
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,  # unbuffered pipes
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    writer_exc: list[BaseException] = []

    def _write_pcm_and_silence() -> None:
        try:
            zero_chunk = b"\x00" * min(io_chunk_bytes, 256 * 1024)

            def write_buf(buf: memoryview) -> None:
                for off in range(0, len(buf), io_chunk_bytes):
                    proc.stdin.write(buf[off : off + io_chunk_bytes])

            def write_silence(nbytes: int) -> None:
                remaining = nbytes
                while remaining > 0:
                    take = min(len(zero_chunk), remaining)
                    proc.stdin.write(zero_chunk[:take])
                    remaining -= take

            for utterance_index, utterance in enumerate(utterances):
                if utterance.get_sample_rate() != target_sr:
                    raise ValueError(f"Sample rate mismatch: utterance {utterance.id} has {utterance.get_sample_rate()}, expected {target_sr}.")

                if utterance_index > 0:
                    write_silence(total_silence_bytes)

                blob = utterance.get_audio_blob()
                if blob is None:
                    raise ValueError(f"Utterance {utterance.id} has no audio_blob.")
                write_buf(memoryview(blob))

        except BaseException as e:
            writer_exc.append(e)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    t = threading.Thread(target=_write_pcm_and_silence, name="ffmpeg_pcm_writer", daemon=True)
    t.start()

    # Read MP3 output while writer thread feeds stdin (prevents deadlocks on full stdout buffers).
    out = io.BytesIO()
    try:
        while True:
            chunk = proc.stdout.read(io_chunk_bytes)
            if not chunk:
                break
            out.write(chunk)

        rc = proc.wait()
        t.join()

        if writer_exc:
            # Prefer writer error (sample-rate mismatch, missing blob, etc.)
            raise writer_exc[0]

        if rc != 0:
            err = (proc.stderr.read() or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg failed (exit {rc}). stderr:\n{err}")

        return out.getvalue()

    finally:
        try:
            proc.kill()
        except Exception:
            pass


def split_transcription_by_utterance(
    transcription_result: Dict[str, Any],
    utterances: Sequence[Utterance],
    *,
    silence_seconds: float = 3.0,
) -> Dict[int, Dict[str, Any]]:
    """
    Split transcription result from a combined MP3 back into per-utterance results.

    Assumes:
      - utterances were concatenated in THIS order
      - each utterance contributes duration_ms / 1000.0 seconds of audio
      - exactly `silence_seconds` of silence was inserted between utterances

    Returns:
      { utterance_id: {"transcript": str, "words": [...], "language": str|None} }
    """
    if not utterances:
        return {}

    language = transcription_result.get("language")
    words = transcription_result.get("words") or []

    # Build utterance time windows in the combined audio.
    windows: List[tuple[int, float, float]] = []
    t = 0.0
    for u in utterances:
        dur_s = u.duration_ms / 1000.0
        start = t
        end = start + dur_s
        windows.append((u.id, start, end))
        t = end + silence_seconds

    output = {utterance.id: {"transcript": "", "words": [], "language": language} for utterance in utterances}

    # Assign each word to the first window it overlaps with.
    word_index = 0
    for window_index, (utterance_id, start, end) in enumerate(windows):
        utterance_words = []
        next_start = windows[window_index + 1][1] if window_index + 1 < len(windows) else None

        while word_index < len(words):
            w = words[word_index]
            # If word starts at or after window end, stop (no overlap with this window)
            if w["start"] >= end:
                break
            # If word ends after window start, it overlaps
            if w["end"] > start:
                word_adjusted = dict(w)
                word_adjusted["start"] = word_adjusted["start"] - start
                # Clamp end to the current window boundary when the word spills
                # into the silence gap or the next utterance (rare with word-level
                # timestamps but happens with slow speech or imprecise Whisper
                # timing). Clamping keeps the word instead of silently dropping it.
                if next_start is not None and w["end"] > next_start:
                    logger.warning(f"Word overlaps with subsequent window, clamping end: {w}")
                    word_adjusted["end"] = end - start
                else:
                    word_adjusted["end"] = word_adjusted["end"] - start
                utterance_words.append(word_adjusted)
            word_index += 1

        output[utterance_id]["words"] = utterance_words
        output[utterance_id]["transcript"] = " ".join(w["word"] for w in utterance_words)

    return output


def get_transcription_via_assemblyai_for_utterance_group(utterances):
    first_utterance = utterances[0]
    total_duration_ms = sum(utterance.duration_ms for utterance in utterances)

    transcription, error = get_transcription_via_assemblyai_from_mp3(
        retrieve_mp3_data_callback=lambda: get_mp3_for_utterance_group(utterances, sample_rate=first_utterance.get_sample_rate()),
        duration_ms=total_duration_ms,
        identifier=f"utterances {[utterance.id for utterance in utterances]}",
        transcription_settings=first_utterance.transcription_settings,
        recording=first_utterance.recording,
    )

    if error:
        return None, error

    return split_transcription_by_utterance(transcription, utterances), None


def _tiny_silence_mp3(seconds: float = 0.3) -> bytes:
    """A few hundred ms of silence as MP3 — just enough to make Whisper load
    its model. Generated with ffmpeg's lavfi anullsrc (no input file needed)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
        "-t", str(seconds), "-c:a", "libmp3lame", "-f", "mp3", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed building warm-up clip: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    return proc.stdout


def warm_up_whisper(recording: Recording, model: str) -> bool:
    """Pre-warm the Cloud Run Whisper service before the real (large) grouped
    requests are sent. The OPENAI_BASE_URL points straight at a scale-to-zero
    Cloud Run instance, so the first request after idle eats a container
    cold-start + model load (~60-90s). Doing that here with a tiny silent clip
    means the real grouped requests hit a warm, model-loaded instance and don't
    risk the per-request timeout — without paying for min-instances=1.

    Best-effort: returns True once the service answers 200, False on timeout.
    Either way the caller proceeds (a missed warm-up just means the first real
    request pays the cold start). Polls because while the container is spinning
    up the endpoint may refuse connections or return 503.
    """
    cred_rec = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not cred_rec:
        return False
    creds = cred_rec.get_credentials()
    if not creds or not creds.get("api_key"):
        return False

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base_url}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {creds['api_key']}"}
    try:
        silence = _tiny_silence_mp3()
    except Exception as e:
        logger.warning(f"whisper warm-up: could not build silence clip: {e}")
        return False

    total_timeout_s = int(os.getenv("WHISPER_WARMUP_TIMEOUT_SECONDS", "180"))
    per_request_timeout_s = int(os.getenv("WHISPER_WARMUP_REQUEST_TIMEOUT_SECONDS", "150"))
    poll_interval_s = 5
    deadline = time.time() + total_timeout_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            files = {
                "file": ("warmup.mp3", silence, "audio/mpeg"),
                "model": (None, model),
                "response_format": (None, "json"),
            }
            r = requests.post(url, headers=headers, files=files, timeout=per_request_timeout_s)
            if r.status_code == 200:
                logger.info(f"whisper warm-up succeeded on attempt {attempt} (model {model})")
                return True
            logger.info(f"whisper warm-up attempt {attempt}: status {r.status_code}")
        except requests.RequestException as e:
            logger.info(f"whisper warm-up attempt {attempt}: {e}")
        time.sleep(poll_interval_s)

    logger.warning(f"whisper warm-up timed out after {total_timeout_s}s; proceeding (first real request pays the cold start)")
    return False


def get_transcription_via_whisper_from_mp3(
    retrieve_mp3_data_callback: Callable[[], bytes],
    duration_ms: int,
    identifier: str,
    transcription_settings: TranscriptionSettings,
    recording: Recording,
):
    """Transcribe one combined MP3 (a time-group of utterances) with Whisper via
    the OpenAI-shaped endpoint. In this deployment `OPENAI_BASE_URL` points
    straight at the Cloud Run faster-whisper service (no rewriting proxy), so the
    `model` field is used verbatim and must be the real loaded model id.

    Returns a dict in the same shape the splitter consumes:
      {"transcript": str, "words": [{"word", "start"(s), "end"(s)}], "language"}.
    Uses plain `verbose_json` (segments with start/end times); when the backend
    surfaces a top-level words array those are used, otherwise each segment
    becomes one coarse "word" entry so the per-utterance splitter still has
    time-stamped text to bucket.
    """
    openai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not openai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}
    openai_credentials = openai_credentials_record.get_credentials()
    if not openai_credentials or not openai_credentials.get("api_key"):
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    # Very short clips are almost never real speech and can make Whisper
    # hallucinate; skip (mirrors the AssemblyAI path's 175ms guard).
    if duration_ms < 175:
        logger.info(f"Whisper transcription skipped for {identifier} because it's less than 175ms in duration")
        return {"transcript": "", "words": [], "language": None}, None

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{base_url}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {openai_credentials['api_key']}"}

    mp3_data = retrieve_mp3_data_callback()
    # Request WORD-level timestamps. split_transcription_by_utterance maps text
    # back to each utterance by time window; with only segment-level granularity
    # a single 2-10s Whisper segment that spans several short (~5s) utterance
    # windows gets dumped entirely into the first window, leaving the others
    # blank (observed: 137/323 utterances empty even though every chunk
    # transcribes cleanly on its own). Word-level timestamps let each word land
    # in its own window, so attribution is even and per-utterance/diarization is
    # correct. NB: word granularity used to return EMPTY words+segments on this
    # speaches backend, which is why it was previously disabled — that was the
    # repetition/temperature bug fixed by the anti-hallucination image patch
    # (temperature fallback + condition_on_previous_text=False). The parser
    # still falls back to segment-level entries if `words` ever comes back empty.
    files = {
        "file": ("file.mp3", mp3_data, "audio/mpeg"),
        "model": (None, transcription_settings.openai_transcription_model()),
        "response_format": (None, "verbose_json"),
        "timestamp_granularities[]": (None, "word"),
        # MUST disable VAD for the grouped path. get_mp3_for_utterance_group
        # concatenates utterances with `silence_seconds` of silence between
        # them, and split_transcription_by_utterance rebuilds per-utterance
        # windows ASSUMING those gaps are present. speaches defaults
        # vad_filter=True, which strips the silences before transcribing and
        # collapses the returned word/segment timestamps — so over many
        # utterances the window timeline drifts (322 gaps x 3s ~= 16 min on a
        # 323-utterance meeting) and most short utterances get assigned no
        # words. With VAD off the timeline is preserved and the split aligns;
        # the anti-hallucination patch on the Whisper image (temperature
        # fallback + no_repeat_ngram + condition_on_previous_text=False) covers
        # any silence-induced looping that VAD would otherwise have removed.
        "vad_filter": (None, "false"),
    }
    if transcription_settings.openai_transcription_language():
        files["language"] = (None, transcription_settings.openai_transcription_language())
    if transcription_settings.openai_transcription_prompt():
        files["prompt"] = (None, transcription_settings.openai_transcription_prompt())

    # faster-whisper-medium on CPU runs ~1.5-2x slower than real-time, so a
    # 6-7 min group can take 600-920s. The old 900s cap let the largest groups
    # time out client-side mid-transcription, triggering a Celery retry that
    # cascaded past the async-transcription max-runtime. 1800s leaves headroom
    # for the largest groups even on a cold (non-warmed) Cloud Run instance.
    timeout_s = int(os.getenv("WHISPER_GROUP_TIMEOUT_SECONDS", "1800"))
    try:
        response = requests.post(url, headers=headers, files=files, timeout=timeout_s)
    except requests.RequestException as e:
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error": str(e)}

    if response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}
    if response.status_code != 200:
        logger.error(f"Whisper group transcription failed ({identifier}) status={response.status_code}: {response.text[:500]}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "text": response.text[:500]}

    result = response.json()
    language = result.get("language")

    formatted_words: List[Dict[str, Any]] = []
    words = result.get("words") or []
    if words:
        for w in words:
            if w.get("start") is None or w.get("end") is None:
                continue
            formatted_words.append({"word": (w.get("word") or "").strip(), "start": float(w["start"]), "end": float(w["end"])})
    else:
        # Word timestamps unavailable — use segments as coarse "word" entries.
        # The splitter buckets by time window, so this still attributes text to
        # the right utterance (just at segment granularity).
        for seg in result.get("segments") or []:
            txt = (seg.get("text") or "").strip()
            if not txt or seg.get("start") is None or seg.get("end") is None:
                continue
            formatted_words.append({"word": txt, "start": float(seg["start"]), "end": float(seg["end"])})

    return {"transcript": result.get("text", ""), "words": formatted_words, "language": language}, None


def get_transcription_via_whisper_for_utterance_group(utterances):
    first_utterance = utterances[0]
    total_duration_ms = sum(utterance.duration_ms for utterance in utterances)

    transcription, error = get_transcription_via_whisper_from_mp3(
        retrieve_mp3_data_callback=lambda: get_mp3_for_utterance_group(utterances, sample_rate=first_utterance.get_sample_rate()),
        duration_ms=total_duration_ms,
        identifier=f"utterances {[utterance.id for utterance in utterances]}",
        transcription_settings=first_utterance.transcription_settings,
        recording=first_utterance.recording,
    )

    if error:
        return None, error

    return split_transcription_by_utterance(transcription, utterances), None


def get_transcription_via_assemblyai_from_mp3(
    retrieve_mp3_data_callback: Callable[[], bytes],
    duration_ms: int,
    identifier: str,
    transcription_settings: TranscriptionSettings,
    recording: Recording,
):
    assemblyai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.ASSEMBLY_AI).first()
    if not assemblyai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    assemblyai_credentials = assemblyai_credentials_record.get_credentials()
    if not assemblyai_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    api_key = assemblyai_credentials.get("api_key")
    if not api_key:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND, "error": "api_key not in credentials"}

    # If the audio blob is less than 175ms in duration, just return an empty transcription
    # Audio clips this short are almost never generated, it almost certainly didn't have any speech
    # and if we send it to the assemblyai api, the upload will fail
    if duration_ms < 175:
        logger.info(f"AssemblyAI transcription skipped for {identifier} because it's less than 175ms in duration")
        return {"transcript": "", "words": []}, None

    headers = {"authorization": api_key}
    base_url = transcription_settings.assemblyai_base_url()

    mp3_data = retrieve_mp3_data_callback()
    upload_response = requests.post(f"{base_url}/upload", headers=headers, data=mp3_data)

    if upload_response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if upload_response.status_code != 200:
        return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "status_code": upload_response.status_code, "text": upload_response.text}

    upload_url = upload_response.json()["upload_url"]

    data = {
        "audio_url": upload_url,
        "speech_models": ["universal-3-pro", "universal-2"],
    }

    if transcription_settings.assembly_ai_language_detection():
        data["language_detection"] = True
    elif transcription_settings.assembly_ai_language_code():
        data["language_code"] = transcription_settings.assembly_ai_language_code()

    # Add keyterms_prompt and speech_model if set
    keyterms_prompt = transcription_settings.assemblyai_keyterms_prompt()
    if keyterms_prompt:
        data["keyterms_prompt"] = keyterms_prompt
    speech_model = transcription_settings.assemblyai_speech_model()
    if speech_model:
        if "speech_models" in data:
            del data["speech_models"]
        data["speech_model"] = speech_model
    speech_models = transcription_settings.assemblyai_speech_models()
    if speech_models:
        if "speech_model" in data:
            del data["speech_model"]
        data["speech_models"] = speech_models

    if transcription_settings.assemblyai_speaker_labels():
        data["speaker_labels"] = True

    language_detection_options = transcription_settings.assemblyai_language_detection_options()
    if language_detection_options:
        data["language_detection_options"] = language_detection_options

    url = f"{base_url}/transcript"
    response = requests.post(url, json=data, headers=headers)

    if response.status_code != 200:
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code, "text": response.text}

    transcript_id = response.json()["id"]
    polling_endpoint = f"{base_url}/transcript/{transcript_id}"

    # Poll the result_url until we get a completed transcription
    max_retries = int(os.getenv("TRANSCRIPTION_POLLING_TIMEOUT_SECONDS", 120))  # Maximum number of retries (2 minutes with 1s sleep)
    retry_count = 0

    while retry_count < max_retries:
        polling_response = requests.get(polling_endpoint, headers=headers)

        if polling_response.status_code != 200:
            logger.error(f"AssemblyAI result fetch failed with status code {polling_response.status_code}")
            time.sleep(10)
            retry_count += 10
            continue

        transcription_result = polling_response.json()

        if transcription_result["status"] == "completed":
            logger.info("AssemblyAI transcription completed successfully, now deleting from AssemblyAI.")

            # Delete the transcript from AssemblyAI
            delete_response = requests.delete(polling_endpoint, headers=headers)
            if delete_response.status_code != 200:
                logger.error(f"AssemblyAI delete failed with status code {delete_response.status_code}: {delete_response.text}")
            else:
                logger.info("AssemblyAI delete successful")

            transcript_text = transcription_result.get("text", "")
            words = transcription_result.get("words", [])

            formatted_words = []
            if words:
                for word in words:
                    formatted_word = {
                        "word": word["text"],
                        "start": word["start"] / 1000.0,
                        "end": word["end"] / 1000.0,
                        "confidence": word["confidence"],
                    }
                    if "speaker" in word:
                        formatted_word["speaker"] = word["speaker"]

                    formatted_words.append(formatted_word)

            transcription = {"transcript": transcript_text, "words": formatted_words, "language": transcription_result.get("language_code", None)}
            return transcription, None

        elif transcription_result["status"] == "error":
            error = transcription_result.get("error")

            if error and "language_detection cannot be performed on files with no spoken audio" in error:
                logger.info(f"AssemblyAI transcription skipped for {identifier} because it did not have any spoken audio and we tried to detect language")
                return {"transcript": "", "words": []}, None

            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error": error}

        else:  # queued, processing
            logger.info(f"AssemblyAI transcription status: {transcription_result['status']}, waiting...")
            time.sleep(1)
            retry_count += 1

    # If we've reached here, we've timed out
    return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "step": "transcribe_result_poll"}
