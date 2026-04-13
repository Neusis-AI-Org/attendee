import logging
import threading
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class HTTPFileUploader:
    """
    Uploads a recording file via HTTP POST (multipart/form-data) to an external URL.
    Same interface as GCSFileUploader so it can be used as a drop-in replacement.

    Robustness improvements:
    - Non-daemon thread (survives main thread exit)
    - 3 retry attempts with 120-second timeout each
    - Logs progress at each stage
    """

    MAX_RETRIES = 3
    UPLOAD_TIMEOUT = 120  # seconds per attempt

    def __init__(self, upload_url, filename, bot_id, bot_object_id, meeting_url=None):
        self.upload_url = upload_url
        self.filename = filename
        self.bot_id = bot_id
        self.bot_object_id = bot_object_id
        self.meeting_url = meeting_url
        self._upload_thread = None
        self._upload_success = False

    def upload_file(self, file_path: str, callback=None):
        self._upload_thread = threading.Thread(
            target=self._upload_worker,
            args=(file_path, callback),
            daemon=False,  # Non-daemon: survives main thread exit
        )
        self._upload_thread.start()

    def _upload_worker(self, file_path: str, callback=None):
        file_path = Path(file_path)
        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            if callback:
                callback(False)
            return

        file_size_mb = file_path.stat().st_size / 1024 / 1024
        logger.info(f"Starting HTTP upload: {file_path.name} ({file_size_mb:.1f} MB) to {self.upload_url}")

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                with open(file_path, "rb") as f:
                    files = {"file": (self.filename, f)}
                    data = {
                        "bot_id": self.bot_object_id,
                        "bot_db_id": str(self.bot_id),
                        "filename": self.filename,
                    }
                    if self.meeting_url:
                        data["meeting_url"] = self.meeting_url

                    logger.info(f"HTTP upload attempt {attempt}/{self.MAX_RETRIES} ({file_size_mb:.1f} MB)...")
                    response = requests.post(
                        self.upload_url,
                        files=files,
                        data=data,
                        timeout=self.UPLOAD_TIMEOUT,
                    )
                    response.raise_for_status()

                logger.info(f"HTTP upload succeeded on attempt {attempt} (status={response.status_code})")
                self._upload_success = True

                if callback:
                    callback(True)
                return

            except requests.Timeout:
                logger.warning(f"HTTP upload attempt {attempt} timed out after {self.UPLOAD_TIMEOUT}s")
            except Exception as e:
                logger.warning(f"HTTP upload attempt {attempt} failed: {e}")

            if attempt < self.MAX_RETRIES:
                import time
                wait = attempt * 5  # 5s, 10s backoff
                logger.info(f"Retrying HTTP upload in {wait}s...")
                time.sleep(wait)

        logger.error(f"HTTP upload failed after {self.MAX_RETRIES} attempts for {file_path.name}")
        if callback:
            callback(False)

    def wait_for_upload(self):
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()

    def delete_file(self, file_path: str):
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
