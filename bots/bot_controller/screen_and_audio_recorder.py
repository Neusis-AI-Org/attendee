import logging
import os
import queue
import subprocess
import threading

logger = logging.getLogger(__name__)

# Sentinel pushed onto the audio queue to signal end-of-stream to the writer
# thread. Distinct from None so callers cannot accidentally trigger shutdown
# by feeding a falsy chunk.
_AUDIO_EOS = object()


class ScreenAndAudioRecorder:
    def __init__(
        self,
        file_location,
        recording_dimensions,
        audio_only,
        audio_source="alsa",
        audio_sample_rate=48000,
    ):
        """
        audio_source:
          - "alsa" (default): ffmpeg captures from the ALSA `default` device,
            which on this image is the PulseAudio sink monitor fed by Chrome's
            mixed meeting audio. Required for Zoom native SDK / RTMS paths and
            any setup where no JS mixed-audio callback is wired up.
          - "pipe": ffmpeg reads raw signed-16-bit little-endian mono PCM from
            stdin. Callers feed chunks via write_audio_chunk(). Used for
            Teams + Google Meet web bots, where the chromedriver payload
            already delivers a clean 48 kHz mixed-audio stream from the
            WebRTC tracks — bypassing the PulseAudio recapture that
            introduces noise, codec round-tripping, and PTS drift.
        audio_sample_rate:
            Hz of the PCM chunks fed via write_audio_chunk(). Only meaningful
            when audio_source == "pipe". Should match the JS-side mixer
            (mixed_audio_sample_rate() on the BotController — 48000 for Teams
            and Meet).
        """
        self.file_location = file_location
        self.ffmpeg_proc = None
        # Screen will have buffer, we will crop to the recording dimensions
        self.screen_dimensions = (recording_dimensions[0] + 10, recording_dimensions[1] + 10)
        self.recording_dimensions = recording_dimensions
        self.audio_only = audio_only
        self.paused = False
        self.xterm_proc = None

        if audio_source not in ("alsa", "pipe"):
            raise ValueError(f"audio_source must be 'alsa' or 'pipe', got {audio_source!r}")
        self.audio_source = audio_source
        self.audio_sample_rate = audio_sample_rate

        # Writer-thread machinery (only used in pipe mode). Chunks arrive on
        # the WebSocket-handling thread and we don't want a slow ffmpeg stdin
        # to back-pressure that thread, so we hand off through a bounded queue
        # and let a dedicated thread do the blocking writes.
        self._audio_queue = None  # type: queue.Queue | None
        self._audio_writer_thread = None  # type: threading.Thread | None
        self._audio_dropped_chunks = 0

    def _build_ffmpeg_cmd(self, display_var):
        if self.audio_only:
            audio_input = self._audio_input_args()
            return [
                "ffmpeg",
                "-y",
                *audio_input,
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                "-ar",
                "44100",
                "-ac",
                "1",
                self.file_location,
            ]

        # Combined screen + audio. The audio-input args change with audio_source;
        # the screen-capture args, the video filter, and the codec stack are
        # identical between modes so the resulting MP4 is byte-compatible from
        # the muxer down.
        audio_input = self._audio_input_args()
        audio_filter = []
        if self.audio_source == "alsa":
            # aresample=async=1000 compensates for ALSA underruns by padding
            # silence so the audio track stays the same duration as the video
            # track. Pointless in pipe mode — the JS side doesn't underrun the
            # same way and any drift there means we want to *see* it in the
            # output, not have ffmpeg silently paper over it.
            audio_filter = ["-af", "aresample=async=1000"]

        return [
            "ffmpeg",
            "-y",
            "-thread_queue_size",
            "256",
            "-framerate",
            "30",
            "-video_size",
            f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}",
            "-f",
            "x11grab",
            "-draw_mouse",
            "0",
            "-probesize",
            "32",
            "-i",
            display_var,
            *audio_input,
            "-vf",
            f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10",
            *audio_filter,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            "-c:a",
            "aac",
            "-strict",
            "experimental",
            "-b:a",
            "128k",
            self.file_location,
        ]

    def _audio_input_args(self):
        """ffmpeg input flags for the audio source. Common to audio-only and combined modes."""
        if self.audio_source == "alsa":
            # The 8192 thread_queue_size on the audio input is the same one the
            # combined-mode comment above describes — it stays here so audio-only
            # mode gets the same drop-resistance.
            return ["-thread_queue_size", "8192", "-f", "alsa", "-i", "default"]
        # pipe: raw s16le mono PCM from stdin at audio_sample_rate. No alsa,
        # no PulseAudio, no recapture round-trip.
        return [
            "-thread_queue_size",
            "8192",
            "-f",
            "s16le",
            "-ar",
            str(self.audio_sample_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
        ]

    def start_recording(self, display_var):
        logger.info(
            f"Starting screen recorder for display {display_var} with dimensions "
            f"{self.screen_dimensions}, file location {self.file_location}, "
            f"audio_source={self.audio_source}"
            + (f" sr={self.audio_sample_rate}" if self.audio_source == "pipe" else "")
        )

        ffmpeg_cmd = self._build_ffmpeg_cmd(display_var)
        logger.info(f"Starting FFmpeg command: {' '.join(ffmpeg_cmd)}")

        if self.audio_source == "pipe":
            self._audio_queue = queue.Queue(maxsize=512)
            self.ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            self._audio_writer_thread = threading.Thread(
                target=self._audio_writer_loop,
                name="screen-and-audio-recorder-stdin",
                daemon=True,
            )
            self._audio_writer_thread.start()
        else:
            self.ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )

    def write_audio_chunk(self, chunk):
        """Enqueue a raw s16le mono PCM chunk to feed into ffmpeg's stdin.
        Safe to call from any thread. No-op (with a single warning) when the
        recorder is in ALSA mode or hasn't been started yet."""
        if self.audio_source != "pipe":
            return
        if self._audio_queue is None or self.ffmpeg_proc is None:
            return
        if self.paused:
            # Drop chunks while paused. Audio track will be shorter than the
            # video track by the pause duration; that mirrors the existing
            # ALSA-pause behaviour where the muted sink emits zero samples for
            # the duration of the pause, so downstream code doesn't need to
            # special-case it.
            return
        try:
            self._audio_queue.put_nowait(chunk)
        except queue.Full:
            # ffmpeg stdin is back-pressuring or the writer thread is stuck.
            # Drop the chunk rather than block the WS-read thread. Log a
            # running count so it's visible without spamming on every drop.
            self._audio_dropped_chunks += 1
            if self._audio_dropped_chunks % 50 == 1:
                logger.warning(
                    f"audio queue full, dropped {self._audio_dropped_chunks} chunks "
                    f"(ffmpeg stdin is not keeping up)"
                )

    def _audio_writer_loop(self):
        """Drain the audio queue into ffmpeg's stdin. Exits cleanly on the EOS
        sentinel pushed by stop_recording, on ffmpeg exit, or on a BrokenPipe
        (ffmpeg died on its own — let stop_recording handle reporting)."""
        proc = self.ffmpeg_proc
        if proc is None or proc.stdin is None:
            return
        stdin = proc.stdin
        try:
            while True:
                item = self._audio_queue.get()
                if item is _AUDIO_EOS:
                    return
                try:
                    stdin.write(item)
                except BrokenPipeError:
                    logger.info("ffmpeg stdin closed; audio writer exiting")
                    return
                except Exception as e:
                    logger.warning(f"audio writer encountered error: {e}")
                    return
        finally:
            try:
                stdin.close()
            except Exception:
                pass

    # Pauses by muting the audio and showing a black xterm covering the entire screen
    def pause_recording(self):
        if self.paused:
            return True  # Already paused, consider this success

        try:
            sw, sh = self.screen_dimensions

            x, y = 0, 0

            self.xterm_proc = subprocess.Popen(["xterm", "-bg", "black", "-fg", "black", "-geometry", f"{sw}x{sh}+{x}+{y}", "-xrm", "*borderWidth:0", "-xrm", "*scrollBar:false"])

            if self.audio_source == "alsa":
                subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"], check=True)
            # In pipe mode write_audio_chunk() short-circuits on self.paused,
            # so we just have to flip the flag — no system mixer to touch.
            self.paused = True
            return True
        except Exception as e:
            logger.error(f"Failed to pause recording: {e}")
            return False

    # Resumes by unmuting the audio and killing the xterm proc
    def resume_recording(self):
        if not self.paused:
            return True

        try:
            self.xterm_proc.terminate()
            self.xterm_proc.wait()
            self.xterm_proc = None
            if self.audio_source == "alsa":
                subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], check=True)
            self.paused = False
            return True
        except Exception as e:
            logger.error(f"Failed to resume recording: {e}")
            return False

    def stop_recording(self):
        if not self.ffmpeg_proc:
            return
        if self.audio_source == "pipe" and self._audio_queue is not None:
            # Push EOS so the writer thread closes ffmpeg stdin cleanly — that
            # signals end-of-input to ffmpeg's muxer, which then flushes the
            # trailing audio and finalises the file. Without this we'd be
            # SIGTERMing ffmpeg while it still has pending audio buffered.
            try:
                self._audio_queue.put_nowait(_AUDIO_EOS)
            except queue.Full:
                # Queue is jammed; force-close stdin so the writer unblocks.
                try:
                    if self.ffmpeg_proc.stdin is not None:
                        self.ffmpeg_proc.stdin.close()
                except Exception:
                    pass
            if self._audio_writer_thread is not None:
                self._audio_writer_thread.join(timeout=5)
        self.ffmpeg_proc.terminate()
        self.ffmpeg_proc.wait()
        self.ffmpeg_proc = None
        if self.audio_source == "pipe" and self._audio_dropped_chunks:
            logger.warning(
                f"recording finished with {self._audio_dropped_chunks} dropped audio chunks"
            )
        logger.info(f"Stopped screen and audio recorder for display with dimensions {self.screen_dimensions} and file location {self.file_location}")

    def get_seekable_path(self, path):
        """
        Transform a file path to include '.seekable' before the extension.
        Example: /tmp/file.webm -> /tmp/file.seekable.webm
        """
        base, ext = os.path.splitext(path)
        return f"{base}.seekable{ext}"

    def cleanup(self):
        input_path = self.file_location

        # If no input path at all, then we aren't trying to generate a file at all
        if input_path is None:
            return

        # Check if input file exists
        if not os.path.exists(input_path):
            logger.info(f"Input file does not exist at {input_path}, creating empty file")
            with open(input_path, "wb"):
                pass  # Create empty file
            return

        # if audio only, we don't need to make it seekable
        if self.audio_only:
            return

        # if input file is greater than 3 GB, we will skip seekability
        if os.path.getsize(input_path) > 3 * 1024 * 1024 * 1024:
            logger.info("Input file is greater than 3 GB, skipping seekability")
            return

        output_path = self.get_seekable_path(self.file_location)
        # the file is seekable, so we don't need to make it seekable
        try:
            self.make_file_seekable(input_path, output_path)
        except Exception as e:
            logger.error(f"Failed to make file seekable: {e}")
            return

    def make_file_seekable(self, input_path, tempfile_path):
        """Use ffmpeg to move the moov atom to the beginning of the file."""
        logger.info(f"Making file seekable: {input_path} -> {tempfile_path}")
        # log how many bytes are in the file
        logger.info(f"File size: {os.path.getsize(input_path)} bytes")
        command = [
            "ffmpeg",
            "-i",
            str(input_path),  # Input file
            "-c",
            "copy",  # Copy streams without re-encoding
            "-avoid_negative_ts",
            "make_zero",  # Optional: Helps ensure timestamps start at or after 0
            "-movflags",
            "+faststart",  # Optimize for web playback
            "-y",  # Overwrite output file without asking
            str(tempfile_path),  # Output file
        ]

        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed to make file seekable: {result.stderr}")

        # Replace the original file with the seekable version
        try:
            os.replace(str(tempfile_path), str(input_path))
            logger.info(f"Replaced original file with seekable version: {input_path}")
        except Exception as e:
            logger.error(f"Failed to replace original file with seekable version: {e}")
            raise RuntimeError(f"Failed to replace original file: {e}")
