#!/bin/sh
# Delete recording files older than $RECORDING_RETENTION_DAYS (default 5).
# Run nightly by the recording-cleanup compose service.
set -eu

RETENTION_DAYS="${RECORDING_RETENTION_DAYS:-5}"
RECORDINGS_DIR="${RECORDINGS_DIR:-/recordings}"

if [ ! -d "$RECORDINGS_DIR" ]; then
	echo "[recording-cleanup] $(date -u +%FT%TZ) skipping: $RECORDINGS_DIR does not exist"
	exit 0
fi

echo "[recording-cleanup] $(date -u +%FT%TZ) scanning $RECORDINGS_DIR for files older than $RETENTION_DAYS days"

# -mtime +N matches files modified more than N days ago. -delete is GNU/POSIX
# busybox-compatible. Lists each file as it goes for the audit log.
find "$RECORDINGS_DIR" -type f -mtime "+$RETENTION_DAYS" -print -delete

echo "[recording-cleanup] $(date -u +%FT%TZ) done"
