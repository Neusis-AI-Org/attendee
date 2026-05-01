import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


class ScreenAndAudioRecorder:
    def __init__(self, file_location, recording_dimensions, audio_only):
        self.file_location = file_location
        self.ffmpeg_proc = None
        # Screen will have buffer, we will crop to the recording dimensions
        self.screen_dimensions = (recording_dimensions[0] + 10, recording_dimensions[1] + 10)
        self.recording_dimensions = recording_dimensions
        self.audio_only = audio_only
        self.paused = False
        self.xterm_proc = None
        self._diag_thread = None
        self._diag_stop = threading.Event()
        self._parec_proc = None

    def start_recording(self, display_var):
        logger.info(f"Starting screen recorder for display {display_var} with dimensions {self.screen_dimensions} and file location {self.file_location}")

        if self.audio_only:
            # FFmpeg command for audio-only recording to MP3.
            # Reverted to the exact form bot 101 used (which captured 44 min),
            # paired with disabling Pulse's module-suspend-on-idle in entrypoint.sh.
            # See bot-flow-architecture.md for the truncation investigation.
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",  # Overwrite output file without asking
                "-thread_queue_size",
                "4096",
                "-f",
                "alsa",  # ALSA input (configured via ~/.asoundrc to use pulse)
                "-i",
                "default",  # Default ALSA device
                "-c:a",
                "libmp3lame",  # MP3 codec
                "-b:a",
                "192k",  # Audio bitrate (192 kbps for good quality)
                "-ar",
                "44100",  # Sample rate
                "-ac",
                "1",  # Mono
                self.file_location,
            ]
        else:
            ffmpeg_cmd = ["ffmpeg", "-y", "-thread_queue_size", "256", "-framerate", "30", "-video_size", f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}", "-f", "x11grab", "-draw_mouse", "0", "-probesize", "32", "-i", display_var, "-thread_queue_size", "4096", "-f", "alsa", "-i", "default", "-vf", f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-strict", "experimental", "-b:a", "128k", self.file_location]

        logger.info(f"Starting FFmpeg command: {' '.join(ffmpeg_cmd)}")
        self.ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # Sidecar parec reader on the same Pulse source. Independent of ffmpeg —
        # if it ALSO stops at ~7.5 min the cap is upstream of ffmpeg (Chrome / Pulse).
        # If it keeps reading while ffmpeg's mp3 freezes, the cap is inside ffmpeg.
        # Output goes to /dev/null; we sample bytes-read via /proc/<pid>/io.rchar.
        if self.audio_only:
            try:
                self._parec_proc = subprocess.Popen(
                    [
                        "parec",
                        "--device=auto_null.monitor",
                        "--raw",
                        "--rate=44100",
                        "--channels=1",
                        "--format=s16le",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info(f"Started parec sidecar pid={self._parec_proc.pid} for upstream-vs-ffmpeg comparison")
            except Exception as e:
                logger.warning(f"Could not start parec sidecar: {e}")
                self._parec_proc = None

        # Diagnostic thread: every 30s, log file growth + PulseAudio pipeline state.
        # This is read-only — used to localize where audio capture stops on long meetings.
        self._diag_stop.clear()
        self._diag_thread = threading.Thread(target=self._audio_pipeline_diagnostic_loop, daemon=True)
        self._diag_thread.start()

    # Pauses by muting the audio and showing a black xterm covering the entire screen
    def pause_recording(self):
        if self.paused:
            return True  # Already paused, consider this success

        try:
            sw, sh = self.screen_dimensions

            x, y = 0, 0

            self.xterm_proc = subprocess.Popen(["xterm", "-bg", "black", "-fg", "black", "-geometry", f"{sw}x{sh}+{x}+{y}", "-xrm", "*borderWidth:0", "-xrm", "*scrollBar:false"])

            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"], check=True)
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
            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], check=True)
            self.paused = False
            return True
        except Exception as e:
            logger.error(f"Failed to resume recording: {e}")
            return False

    def check_process_health(self):
        """Check if the FFmpeg process is still alive. Returns True if healthy, False if dead."""
        if not self.ffmpeg_proc:
            return True  # No process to check (recording not started or already stopped)

        returncode = self.ffmpeg_proc.poll()
        if returncode is not None:
            # FFmpeg has exited unexpectedly
            stderr_output = ""
            if self.ffmpeg_proc.stderr:
                try:
                    stderr_output = self.ffmpeg_proc.stderr.read().decode("utf-8", errors="replace")
                except Exception:
                    stderr_output = "<unable to read stderr>"
            logger.error(f"FFmpeg process exited unexpectedly with return code {returncode}. stderr: {stderr_output}")
            self.ffmpeg_proc = None
            return False

        return True

    def _audio_pipeline_diagnostic_loop(self):
        last_size = -1
        last_growth_at = time.time()
        started_at = time.time()
        last_parec_rchar = -1
        last_parec_growth_at = time.time()
        while not self._diag_stop.wait(30):
            try:
                size = os.path.getsize(self.file_location) if os.path.exists(self.file_location) else -1
                growing = size > last_size
                if growing:
                    last_growth_at = time.time()
                stalled_for = int(time.time() - last_growth_at) if not growing else 0
                elapsed = int(time.time() - started_at)

                # Sidecar parec process — tracks whether Pulse is still delivering
                # samples to ANYONE on the same source. If parec keeps reading while
                # ffmpeg's mp3 freezes, the bug is inside ffmpeg.
                parec_rchar = -1
                parec_growing = "n/a"
                parec_stalled = 0
                parec_alive = "no_proc"
                if self._parec_proc is not None:
                    if self._parec_proc.poll() is None:
                        parec_alive = "running"
                        try:
                            # Track wchar: parec reads audio from a Pulse socket and writes
                            # it to stdout (/dev/null). Socket reads don't count in rchar,
                            # but the write side does — wchar is the real audio-flow signal.
                            with open(f"/proc/{self._parec_proc.pid}/io") as f:
                                for line in f:
                                    if line.startswith("wchar:"):
                                        parec_rchar = int(line.split()[1])
                                        break
                            if parec_rchar > last_parec_rchar:
                                parec_growing = "True"
                                last_parec_growth_at = time.time()
                            else:
                                parec_growing = "False"
                            parec_stalled = int(time.time() - last_parec_growth_at) if parec_growing == "False" else 0
                            last_parec_rchar = parec_rchar
                        except Exception as e:
                            parec_alive = f"io_err:{e}"
                    else:
                        parec_alive = f"exited_rc={self._parec_proc.returncode}"

                def pactl(args, timeout=3):
                    try:
                        out = subprocess.check_output(["pactl"] + args, stderr=subprocess.STDOUT, timeout=timeout)
                        return out.decode("utf-8", "replace").strip()
                    except Exception as e:
                        return f"<pactl error: {e}>"

                sink_inputs = pactl(["list", "short", "sink-inputs"])  # Chrome -> auto_null
                source_outputs = pactl(["list", "short", "source-outputs"])  # ffmpeg + parec <- auto_null.monitor
                sinks = pactl(["list", "short", "sinks"])

                # Look for "CORKED" / "SUSPENDED" markers in the long form for sink-inputs
                sink_input_full = pactl(["list", "sink-inputs"])
                corked = "Corked: yes" in sink_input_full
                suspended_marker = "RUNNING" not in (pactl(["list", "sinks"]) or "")

                logger.info(
                    "AUDIO_DIAG t+%ds size=%d growing=%s stalled_for=%ds | parec=%s rchar=%d parec_growing=%s parec_stalled=%ds | corked=%s sink_running=%s | sink_inputs=[%s] | source_outputs=[%s] | sinks=[%s]",
                    elapsed,
                    size,
                    growing,
                    stalled_for,
                    parec_alive,
                    parec_rchar,
                    parec_growing,
                    parec_stalled,
                    corked,
                    not suspended_marker,
                    sink_inputs.replace("\n", " | "),
                    source_outputs.replace("\n", " | "),
                    sinks.replace("\n", " | "),
                )
                last_size = size
            except Exception:
                logger.exception("AUDIO_DIAG loop error")

    def stop_recording(self):
        if not self.ffmpeg_proc:
            return
        # Stop diagnostic thread first so it doesn't race with ffmpeg shutdown
        self._diag_stop.set()
        if self._diag_thread and self._diag_thread.is_alive():
            self._diag_thread.join(timeout=2)
        # Stop parec sidecar (read-only, just for diagnostics)
        if self._parec_proc and self._parec_proc.poll() is None:
            try:
                self._parec_proc.terminate()
                self._parec_proc.wait(timeout=3)
            except Exception:
                try: self._parec_proc.kill()
                except Exception: pass
            self._parec_proc = None
        # ffmpeg can block in an uninterruptible ALSA read syscall and ignore SIGTERM.
        # Without a timeout this would hang cleanup() indefinitely and the pod would
        # be SIGKILLed before the recording is uploaded.
        self.ffmpeg_proc.terminate()
        try:
            self.ffmpeg_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg did not exit within 15s of SIGTERM, sending SIGKILL")
            self.ffmpeg_proc.kill()
            try:
                self.ffmpeg_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.error("FFmpeg did not exit within 5s of SIGKILL — abandoning")
        # Log any FFmpeg output on normal shutdown
        if self.ffmpeg_proc.stderr:
            try:
                stderr_output = self.ffmpeg_proc.stderr.read().decode("utf-8", errors="replace")
                if stderr_output.strip():
                    logger.info(f"FFmpeg stderr on shutdown: {stderr_output[-2000:]}")
            except Exception:
                pass
        self.ffmpeg_proc = None
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

        # Check if input file exists — do NOT create an empty file here.
        # Creating an empty file causes the upload logic to overwrite
        # existing recordings with empty content. See issue #587.
        if not os.path.exists(input_path):
            logger.info(f"Input file does not exist at {input_path}, skipping cleanup")
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
