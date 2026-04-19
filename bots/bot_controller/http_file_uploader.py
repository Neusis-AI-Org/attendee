import logging
import os
import threading
from pathlib import Path

import requests

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class HTTPFileUploader:
    """
    Uploads a recording file to GCS (via Workload Identity) and then
    notifies the bot-scheduler via a lightweight HTTP POST with the GCS path.

    Falls back to direct HTTP multipart upload for files under 30MB.

    Robustness improvements:
    - Non-daemon thread (survives main thread exit)
    - GCS upload for large files (no size limit)
    - 3 retry attempts for HTTP notification
    - Logs progress at each stage
    """

    MAX_RETRIES = 3
    UPLOAD_TIMEOUT = 120  # seconds per attempt
    DIRECT_UPLOAD_MAX_BYTES = 30 * 1024 * 1024  # 30MB — Cloud Run limit

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

        file_size = file_path.stat().st_size
        file_size_mb = file_size / 1024 / 1024
        logger.info(f"Starting upload: {file_path.name} ({file_size_mb:.1f} MB)")

        if file_size > self.DIRECT_UPLOAD_MAX_BYTES:
            # Large file: upload to GCS first, then notify
            success = self._upload_via_gcs(file_path, file_size_mb)
        else:
            # Small file: direct HTTP multipart upload
            success = self._upload_direct(file_path, file_size_mb)

        self._upload_success = success
        if callback:
            callback(success)

    def _upload_via_gcs(self, file_path: Path, file_size_mb: float) -> bool:
        """Upload large file to GCS, then send lightweight notification."""
        gcs_bucket = os.getenv("GCS_RECORDING_BUCKET") or os.getenv("AWS_RECORDING_STORAGE_BUCKET_NAME")
        gcs_project = os.getenv("GCS_PROJECT_ID")

        if not gcs_bucket or not gcs_project:
            logger.warning(f"GCS not configured, falling back to direct upload for {file_size_mb:.1f} MB file")
            return self._upload_direct(file_path, file_size_mb)

        try:
            from google.cloud import storage
            client = storage.Client(project=gcs_project)
            bucket = client.bucket(gcs_bucket)
            gcs_path = f"recordings/{self.bot_object_id}-{self.filename}"
            blob = bucket.blob(gcs_path)

            logger.info(f"Uploading {file_size_mb:.1f} MB to GCS: gs://{gcs_bucket}/{gcs_path}")
            blob.upload_from_filename(str(file_path), content_type="audio/mpeg")
            logger.info(f"GCS upload complete: gs://{gcs_bucket}/{gcs_path}")

            # Notify bot-scheduler with GCS path (lightweight, no file data)
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    response = requests.post(
                        self.upload_url,
                        json={
                            "bot_id": self.bot_object_id,
                            "bot_db_id": str(self.bot_id),
                            "filename": self.filename,
                            "meeting_url": self.meeting_url,
                            "gcs_path": f"gs://{gcs_bucket}/{gcs_path}",
                        },
                        timeout=30,
                    )
                    if response.ok:
                        logger.info(f"Bot-scheduler notified of GCS upload (attempt {attempt})")
                        return True
                    logger.warning(f"Notification attempt {attempt} failed: {response.status_code}")
                except Exception as e:
                    logger.warning(f"Notification attempt {attempt} failed: {e}")

                if attempt < self.MAX_RETRIES:
                    import time
                    time.sleep(attempt * 5)

            # Even if notification fails, recording is safe in GCS
            logger.warning("Notification failed but recording is in GCS — bot-scheduler can retrieve it later")
            return True

        except Exception as e:
            logger.error(f"GCS upload failed: {e}, falling back to direct upload")
            return self._upload_direct(file_path, file_size_mb)

    def _upload_direct(self, file_path: Path, file_size_mb: float) -> bool:
        """Direct HTTP multipart upload (for files under 30MB)."""
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

                    logger.info(f"Direct HTTP upload attempt {attempt}/{self.MAX_RETRIES} ({file_size_mb:.1f} MB)...")
                    response = requests.post(
                        self.upload_url,
                        files=files,
                        data=data,
                        timeout=self.UPLOAD_TIMEOUT,
                    )
                    response.raise_for_status()

                logger.info(f"Direct upload succeeded on attempt {attempt}")
                return True

            except requests.Timeout:
                logger.warning(f"Direct upload attempt {attempt} timed out after {self.UPLOAD_TIMEOUT}s")
            except Exception as e:
                logger.warning(f"Direct upload attempt {attempt} failed: {e}")

            if attempt < self.MAX_RETRIES:
                import time
                time.sleep(attempt * 5)

        logger.error(f"Direct upload failed after {self.MAX_RETRIES} attempts for {file_path.name}")
        return False

    def wait_for_upload(self):
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()

    def delete_file(self, file_path: str):
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
