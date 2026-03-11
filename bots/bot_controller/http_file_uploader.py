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
    """

    def __init__(self, upload_url, filename, bot_id, bot_object_id, meeting_url=None):
        self.upload_url = upload_url
        self.filename = filename
        self.bot_id = bot_id
        self.bot_object_id = bot_object_id
        self.meeting_url = meeting_url
        self._upload_thread = None
        self._upload_success = False

    def upload_file(self, file_path: str, callback=None):
        self._upload_thread = threading.Thread(target=self._upload_worker, args=(file_path, callback), daemon=True)
        self._upload_thread.start()

    def _upload_worker(self, file_path: str, callback=None):
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                raise FileNotFoundError(f"File not found: {file_path}")

            with open(file_path, "rb") as f:
                files = {"file": (self.filename, f)}
                data = {
                    "bot_id": self.bot_object_id,
                    "bot_db_id": str(self.bot_id),
                    "filename": self.filename,
                }
                if self.meeting_url:
                    data["meeting_url"] = self.meeting_url

                response = requests.post(self.upload_url, files=files, data=data, timeout=300)
                response.raise_for_status()

            logger.info(f"Successfully uploaded {file_path} to {self.upload_url} (status={response.status_code})")
            self._upload_success = True

            if callback:
                callback(True)

        except Exception as e:
            logger.error(f"HTTP upload error: {e}")
            if callback:
                callback(False)

    def wait_for_upload(self):
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()

    def delete_file(self, file_path: str):
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
