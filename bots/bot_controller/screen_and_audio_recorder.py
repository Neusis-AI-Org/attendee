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
        # Audio-only path uses parec | lame instead of ffmpeg to bypass ffmpeg's
        # ~10 min input-side cap. See docs/gcp-deployment/audio-truncation-fix.md.
        self._parec_proc = None
        self._lame_proc = None

    def start_recording(self, display_var):
        logger.info(f"Starting screen recorder for display {display_var} with dimensions {self.screen_dimensions} and file location {self.file_location}")

        if self.audio_only:
            # Bypass ffmpeg entirely for audio capture. ffmpeg's libavformat
            # input-side state hits a ~10 min cap that segment muxer rotation
            # cannot defeat. parec is a tiny Pulse client that we already
            # verified runs unbounded; lame is a standalone mp3 encoder that
            # reads raw PCM from stdin. Together they form a much simpler
            # pipeline with none of ffmpeg's internal state to corrupt.
            #
            #   parec --device=auto_null.monitor --raw ... | lame -r ... - <out.mp3>
            parec_cmd = [
                "parec",
                "--device=auto_null.monitor",
                "--raw",
                "--rate=44100",
                "--channels=1",
                "--format=s16le",
            ]
            lame_cmd = [
                "lame",
                "-r",  # raw PCM input
                "--bitwidth", "16",
                "--signed",
                "--little-endian",
                "-s", "44.1",  # sample rate (kHz)
                "-m", "m",     # mono
                "-b", "192",   # bitrate
                "--quiet",
                "-",
                self.file_location,
            ]
            logger.info(f"Starting audio capture pipeline: {' '.join(parec_cmd)} | {' '.join(lame_cmd)}")
            self._parec_proc = subprocess.Popen(parec_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self._lame_proc = subprocess.Popen(lame_cmd, stdin=self._parec_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            # Close parent's reference to parec's stdout pipe so lame's exit
            # propagates SIGPIPE to parec correctly on shutdown.
            self._parec_proc.stdout.close()
            # ffmpeg_proc stays None for audio-only; check_process_health is keyed on it
            # but the pause/resume path uses pactl set-sink-mute which works the same way.
            self.ffmpeg_proc = None
        else:
            ffmpeg_cmd = ["ffmpeg", "-y", "-thread_queue_size", "256", "-framerate", "30", "-video_size", f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}", "-f", "x11grab", "-draw_mouse", "0", "-probesize", "32", "-i", display_var, "-thread_queue_size", "4096", "-f", "alsa", "-i", "default", "-vf", f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-strict", "experimental", "-b:a", "128k", self.file_location]
            logger.info(f"Starting FFmpeg command: {' '.join(ffmpeg_cmd)}")
            self.ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # Diagnostic thread: every 30s, log file growth so a future regression is
        # immediately visible (the thread that found the original ffmpeg cap).
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
        """Check if the recording process(es) are alive. Audio-only uses
        parec | lame; video uses ffmpeg. Returns True if healthy, False if dead."""
        # Audio-only path: both parec and lame must be alive
        if self._parec_proc is not None or self._lame_proc is not None:
            for name, proc in (("parec", self._parec_proc), ("lame", self._lame_proc)):
                if proc is None:
                    continue
                rc = proc.poll()
                if rc is not None:
                    logger.error(f"{name} process exited unexpectedly with return code {rc}")
                    return False
            return True

        # Video path: ffmpeg
        if not self.ffmpeg_proc:
            return True  # not started or already stopped
        returncode = self.ffmpeg_proc.poll()
        if returncode is not None:
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
        """Every 30s, log file growth so a future regression in audio capture is
        immediately visible in pod logs. This is the diagnostic that found the
        original ffmpeg ~10 min cap; keeping it as a tripwire."""
        last_size = -1
        last_growth_at = time.time()
        started_at = time.time()
        while not self._diag_stop.wait(30):
            try:
                size = os.path.getsize(self.file_location) if os.path.exists(self.file_location) else -1
                growing = size > last_size
                if growing:
                    last_growth_at = time.time()
                stalled_for = int(time.time() - last_growth_at) if not growing else 0
                elapsed = int(time.time() - started_at)

                parec_alive = "no_proc"
                if self._parec_proc is not None:
                    parec_alive = "running" if self._parec_proc.poll() is None else f"exited_rc={self._parec_proc.returncode}"
                lame_alive = "no_proc"
                if self._lame_proc is not None:
                    lame_alive = "running" if self._lame_proc.poll() is None else f"exited_rc={self._lame_proc.returncode}"

                logger.info(
                    "AUDIO_DIAG t+%ds size=%d growing=%s stalled_for=%ds | parec=%s | lame=%s",
                    elapsed, size, growing, stalled_for, parec_alive, lame_alive,
                )
                last_size = size
            except Exception:
                logger.exception("AUDIO_DIAG loop error")

    def stop_recording(self):
        if not (self.ffmpeg_proc or self._lame_proc or self._parec_proc):
            return
        # Stop diagnostic thread first so it doesn't race with shutdown
        self._diag_stop.set()
        if self._diag_thread and self._diag_thread.is_alive():
            self._diag_thread.join(timeout=2)

        # Audio-only path: shut down parec first so lame sees EOF on stdin and
        # finalizes the mp3 cleanly. lame's own SIGTERM also works but EOF gives
        # the cleanest mp3 frame closure.
        if self._parec_proc is not None:
            try:
                self._parec_proc.terminate()
                self._parec_proc.wait(timeout=5)
            except Exception:
                try: self._parec_proc.kill()
                except Exception: pass
            logger.info("parec stopped")
            self._parec_proc = None
        if self._lame_proc is not None:
            try:
                # lame should exit on EOF from parec; give it a moment then SIGTERM if still alive
                self._lame_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("lame did not exit on EOF within 10s, sending SIGTERM")
                self._lame_proc.terminate()
                try:
                    self._lame_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("lame did not exit on SIGTERM, sending SIGKILL")
                    self._lame_proc.kill()
            if self._lame_proc.stderr:
                try:
                    stderr_output = self._lame_proc.stderr.read().decode("utf-8", errors="replace")
                    if stderr_output.strip():
                        logger.info(f"lame stderr on shutdown: {stderr_output[-2000:]}")
                except Exception:
                    pass
            self._lame_proc = None
            logger.info(f"Stopped audio capture pipeline (parec | lame) → {self.file_location}")

        # Video path: ffmpeg can block in an uninterruptible ALSA read syscall and
        # ignore SIGTERM. Without a timeout this would hang cleanup() indefinitely
        # and the pod would be SIGKILLed before the recording is uploaded.
        if self.ffmpeg_proc is not None:
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
