import logging
import threading
from pathlib import Path

from google.cloud import storage

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class GCSFileUploader:
    def __init__(self, bucket, filename):
        self.client = storage.Client()
        self.bucket_name = bucket
        self.filename = filename
        self._upload_thread = None

    def upload_file(self, file_path: str, callback=None):
        self._upload_thread = threading.Thread(target=self._upload_worker, args=(file_path, callback), daemon=True)
        self._upload_thread.start()

    def _upload_worker(self, file_path: str, callback=None):
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                raise FileNotFoundError(f"File not found: {file_path}")

            bucket = self.client.bucket(self.bucket_name)
            blob = bucket.blob(self.filename)
            blob.upload_from_filename(str(file_path))

            logger.info(f"Successfully uploaded {file_path} to gs://{self.bucket_name}/{self.filename}")

            if callback:
                callback(True)

        except Exception as e:
            logger.error(f"Upload error: {e}")
            if callback:
                callback(False)

    def wait_for_upload(self):
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()

    def delete_file(self, file_path: str):
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
