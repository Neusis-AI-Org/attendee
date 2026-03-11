# Attendee GCP Deployment Guide

Step-by-step record of deploying Attendee on GCP (project: `neusis-platform`) with GKE Autopilot, Cloud SQL, Memorystore Redis, GCS, and Microsoft calendar integration.

---

## 1. Infrastructure Provisioning (Terraform)

**File:** `deploy/terraform/main.tf`

Provisioned the following resources in `us-central1`:

| Resource | Name | Details |
|---|---|---|
| VPC | `attendee-vpc` | Custom subnet `10.0.0.0/20`, pod range `10.4.0.0/14`, services range `10.8.0.0/20` |
| GKE Autopilot | `attendee-cluster` | Private nodes, public control plane, REGULAR release channel |
| Cloud SQL | `attendee-postgres-production` | PostgreSQL 15, private IP `10.251.1.2`, 200 max connections |
| Memorystore | `attendee-redis-production` | Redis 7.0, private IP `10.251.0.4` |
| GCS Bucket | `attendee-recordings-neusis-platform` | Versioned, 90-day lifecycle to Nearline |
| Artifact Registry | `attendee` | Docker format, `us-central1` |
| Cloud NAT | `attendee-nat` via `attendee-router` | Outbound internet for private GKE nodes |
| Service Accounts | `attendee-app`, `attendee-bot` | Workload Identity bindings to GKE SAs |

### Cloud NAT (added to fix outbound connectivity)

GKE Autopilot with private nodes has no outbound internet by default. This caused `tldextract` (used by `bots/meeting_url_utils.py`) to timeout downloading the public suffix list, resulting in 502 errors when creating bots.

**Fix:** Added Cloud NAT to `main.tf`:

```terraform
resource "google_compute_router" "router" {
  name    = "attendee-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name                               = "attendee-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}
```

Also created via gcloud for immediate effect (Terraform captures the declarative state):

```bash
gcloud compute routers create attendee-router --network=attendee-vpc --region=us-central1
gcloud compute routers nats create attendee-nat --router=attendee-router --region=us-central1 \
  --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges
```

---

## 2. Native GCS Storage Backend

Attendee originally supported S3 and Azure storage. GKE Workload Identity provides GCP credentials natively (no access keys), but the S3 backend with `USE_IRSA_FOR_S3_STORAGE` expects AWS-style credentials. HMAC keys were blocked by the org policy `iam.disableServiceAccountKeyCreation`.

**Solution:** Implemented a native GCS storage backend using `django-storages` GoogleCloudStorage and `google-cloud-storage` SDK.

### Files changed:

#### `requirements.txt`
Added `google-cloud-storage==2.19.0`.

#### `attendee/settings/base.py`
Added GCS storage protocol case between Azure and S3 (default):

```python
elif STORAGE_PROTOCOL == "gcs":
    DEFAULT_STORAGE_BACKEND = {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "project_id": os.getenv("GCS_PROJECT_ID"),
        },
    }
    RECORDING_STORAGE_BACKEND = copy.deepcopy(DEFAULT_STORAGE_BACKEND)
    RECORDING_STORAGE_BACKEND["OPTIONS"]["bucket_name"] = AWS_RECORDING_STORAGE_BUCKET_NAME

    AUDIO_CHUNK_STORAGE_BACKEND = copy.deepcopy(DEFAULT_STORAGE_BACKEND)
    AUDIO_CHUNK_STORAGE_BACKEND["OPTIONS"]["bucket_name"] = AWS_AUDIO_CHUNK_STORAGE_BUCKET_NAME
```

Authentication is handled automatically by Workload Identity (no keys needed).

> **Known Limitation:** GCS signed URLs do not work with Workload Identity. The `generate_signed_url()` call in `django-storages` requires a private key, but Workload Identity provides `google.auth.compute_engine.credentials.Credentials` (token-only). This causes `AttributeError: you need a private key to sign credentials` when the recording download API attempts to generate a signed URL. **Workaround:** Use HTTP recording upload (see Section 10a) to POST recordings directly to your application instead of serving them from GCS.

#### `bots/bot_controller/gcs_file_uploader.py` (new file)
GCS file uploader using `google.cloud.storage` with threaded upload, matching the same interface as `S3FileUploader` and `AzureFileUploader`:

- `upload_file(file_path, callback)` - threaded upload
- `wait_for_upload()` - join upload thread
- `delete_file(file_path)` - remove local file

#### `bots/bot_controller/bot_controller.py`
Added import for `GCSFileUploader` and GCS case to `get_file_uploader()`:

```python
if settings.STORAGE_PROTOCOL == "gcs":
    return GCSFileUploader(
        bucket=settings.AWS_RECORDING_STORAGE_BUCKET_NAME,
        filename=self.get_recording_filename(),
    )
```

#### `bots/storage.py`
Updated `remote_storage_url()` to handle GCS the same as Azure (direct URL, no presigned URL needed):

```python
if settings.STORAGE_PROTOCOL in ("azure", "gcs"):
    return file_field.url
```

#### `bots/models.py`
Updated both `url` properties (`Recording` at line ~2096 and `BotDebugScreenshot` at line ~2824):

```python
if settings.STORAGE_PROTOCOL in ("azure", "gcs"):
    return self.file.url
```

---

## 3. Kubernetes Configuration

### `deploy/k8s/configmap.yaml`

Non-secret environment variables:

```yaml
STORAGE_PROTOCOL: "gcs"
AWS_RECORDING_STORAGE_BUCKET_NAME: "attendee-recordings-neusis-platform"
GCS_PROJECT_ID: "neusis-platform"
LAUNCH_BOT_METHOD: "kubernetes"
BOT_POD_IMAGE: "us-central1-docker.pkg.dev/neusis-platform/attendee/attendee"
BOT_POD_SERVICE_ACCOUNT_NAME: "attendee-bot"
DJANGO_SETTINGS_MODULE: "attendee.settings.production-gke"
RECORDING_UPLOAD_URL: "https://<your-bot-handler>/api/recordings/upload"
```

> **Important:** `BOT_POD_IMAGE` must **not** include a Docker tag. The tag is appended automatically from `CUBER_RELEASE_VERSION` (in secret.yaml). Setting both produces an invalid double-tag like `attendee:http-upload:no-auto-create` → `InvalidImageName` error on bot pod creation.

### `deploy/k8s/secret.yaml` (gitignored)

Contains sensitive values: `DATABASE_URL`, `REDIS_URL`, `DJANGO_SECRET_KEY`, `CREDENTIALS_ENCRYPTION_KEY`, `CUBER_RELEASE_VERSION`.

`CUBER_RELEASE_VERSION` must match the Docker image tag you pushed. For example, if you pushed `attendee:http-upload`, set `CUBER_RELEASE_VERSION: "http-upload"`.

### `deploy/k8s/deployments.yaml`

Three deployments in namespace `attendee`:

| Deployment | Replicas | Purpose |
|---|---|---|
| `attendee-web` | 2 | Gunicorn (Django API server) with health probes |
| `attendee-worker` | 2 | Celery worker (4 concurrency) for async tasks |
| `attendee-scheduler` | 1 | Celery beat scheduler |

All use `serviceAccountName: attendee-app` with Workload Identity.

> **Note:** ConfigMap/Secret changes are NOT automatically picked up by running pods. After updating configmap or secret, you must restart deployments:
> ```bash
> kubectl rollout restart deployment/attendee-web deployment/attendee-worker deployment/attendee-scheduler -n attendee
> ```

### Workload Identity Setup (kubectl)

```bash
# Create GKE service accounts
kubectl create serviceaccount attendee-app -n attendee
kubectl create serviceaccount attendee-bot -n attendee

# Annotate with GCP SA emails
kubectl annotate serviceaccount attendee-app -n attendee \
  iam.gke.io/gcp-service-account=attendee-app@neusis-platform.iam.gserviceaccount.com
kubectl annotate serviceaccount attendee-bot -n attendee \
  iam.gke.io/gcp-service-account=attendee-bot@neusis-platform.iam.gserviceaccount.com
```

---

## 4. Docker Build and Deploy

```bash
# Build for linux/amd64 (GKE nodes)
docker build --platform linux/amd64 -t us-central1-docker.pkg.dev/neusis-platform/attendee/attendee:<tag> .

# Authenticate and push
gcloud auth configure-docker us-central1-docker.pkg.dev
docker push us-central1-docker.pkg.dev/neusis-platform/attendee/attendee:<tag>

# Apply k8s manifests
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/secret.yaml

# Update deployment images
kubectl set image deployment/attendee-web web=us-central1-docker.pkg.dev/neusis-platform/attendee/attendee:<tag> -n attendee
kubectl set image deployment/attendee-worker worker=us-central1-docker.pkg.dev/neusis-platform/attendee/attendee:<tag> -n attendee
kubectl set image deployment/attendee-scheduler scheduler=us-central1-docker.pkg.dev/neusis-platform/attendee/attendee:<tag> -n attendee
```

**Note:** When re-pushing the same tag, use a new tag name (e.g. `calendar-fix-v2`) to force GKE to pull the updated image. Kubernetes caches images by tag.

---

## 5. Microsoft Calendar Integration

### Overview

Attendee syncs with Microsoft Outlook calendars via Microsoft Graph API to automatically pull meeting events. The flow:

1. Register an OAuth app in Microsoft Entra (Azure AD)
2. User authorizes the app via OAuth, producing a refresh token
3. Create a Calendar in Attendee via API with the credentials
4. Attendee's scheduler periodically syncs events via Graph API `/me/calendarView`
5. Synced events with meeting URLs can have bots created for them

### Microsoft Entra App Registration

- **App (client) ID:** `245fefd8-435f-493a-bbbb-c304e1aec7b1`
- **Tenant ID:** `f62b7d25-2154-4dfc-a7ae-4824cd99b146`
- **Tenant type:** Single-tenant (could not change to multi-tenant due to Entra bug: "Property api.requestedAccessTokenVersion is invalid")
- **Redirect URI:** `http://localhost:3001/api/calendar/auth/callback`
- **API Permissions:** `Calendars.Read`, `User.Read`, `offline_access`

### OAuth Token Acquisition

Used a local helper script (`get_ms_token.py`, gitignored) to run the OAuth flow:

1. Starts HTTP server on `localhost:3001`
2. Opens browser for Microsoft login
3. Receives OAuth callback with authorization code
4. Exchanges code for access + refresh tokens using tenant-specific endpoint
5. Saves refresh token to `/tmp/ms_calendar_token.json`

### Calendar Creation (API)

```bash
curl -X POST http://<IP>/api/v1/calendars \
  -H "Authorization: Token <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "platform": "microsoft",
    "client_id": "<ENTRA_CLIENT_ID>",
    "client_secret": "<ENTRA_CLIENT_SECRET>",
    "refresh_token": "<REFRESH_TOKEN>",
    "metadata": {"tenant_id": "<TENANT_ID>"},
    "deduplication_key": "bot-neusis@neusis.ai"
  }'
```

### Code Changes for Single-Tenant Support

**File:** `bots/tasks/sync_calendar_task.py`

**Problem:** The sync handler hardcoded `https://login.microsoftonline.com/common/oauth2/v2.0/token` as the token endpoint. The `/common` endpoint only works for multi-tenant apps. Single-tenant apps get error `AADSTS50194`.

**Fix:** Changed `TOKEN_URL` class constant to a `_token_url` property that reads `tenant_id` from calendar metadata or credentials:

```python
DEFAULT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

@property
def _token_url(self):
    tenant_id = None
    if self.calendar.metadata and isinstance(self.calendar.metadata, dict):
        tenant_id = self.calendar.metadata.get("tenant_id")
    if not tenant_id:
        credentials = self.calendar.get_credentials()
        if credentials:
            tenant_id = credentials.get("tenant_id")
    if tenant_id:
        return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    return self.DEFAULT_TOKEN_URL
```

Falls back to `/common` for multi-tenant apps that don't set a `tenant_id`.

### Non-Blocking Notification Channel Failure

**Problem:** Microsoft Graph webhook subscriptions require a valid HTTPS `notificationUrl`. Without a domain and SSL configured (`SITE_DOMAIN: "*"`), the subscription creation fails and blocks the entire calendar sync.

**Fix:** Wrapped `_refresh_notification_channels()` in a try/except so the poll-based event sync continues even when webhooks can't be set up:

```python
try:
    self._refresh_notification_channels()
except Exception as e:
    logger.warning("Calendar %s: Failed to refresh notification channels, "
                   "continuing with poll-based sync: %s", self.calendar.object_id, e)
```

Events are still synced by the scheduler's periodic polling (every few minutes).

---

## 6. Fernet Encryption Key

Calendar OAuth credentials are encrypted at rest using Fernet symmetric encryption (`CREDENTIALS_ENCRYPTION_KEY` env var).

**Requirement:** Must be exactly 32 url-safe base64-encoded bytes (44 chars with `=` padding).

**Generate a valid key:**

```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

---

## 7. Issues Encountered and Resolutions

| Issue | Root Cause | Resolution |
|---|---|---|
| Bot creation 502 | `tldextract` timeout downloading public suffix list (no outbound internet) | Added Cloud NAT for private GKE nodes |
| Recording upload "Unable to locate credentials" | S3 backend expects AWS credentials; Workload Identity provides GCP tokens | Implemented native GCS storage backend |
| Docker build "No space left on device" | Local Docker disk full | `docker system prune -af` |
| AADSTS50194 multi-tenant error | Entra app is single-tenant, code used `/common` endpoint | Changed to tenant-specific endpoint via `_token_url` property |
| Fernet key invalid | `CREDENTIALS_ENCRYPTION_KEY` was 43 chars (missing padding `=`) | Generated proper Fernet key with `Fernet.generate_key()` |
| Calendar sync blocked by webhook failure | `SITE_DOMAIN: "*"` produces invalid notification URL | Made notification channel creation non-fatal |
| Image not updated after push | Same tag cached by GKE container runtime | Used a new tag name to force pull |
| HMAC keys blocked | Org policy `iam.disableServiceAccountKeyCreation` | Used native GCS + Workload Identity instead |
| Recording lost when meeting ends | Bot pod killed before GCS upload finished (60s grace period too short) | Increased `termination_grace_period_seconds` from 60 to 300 |
| GCS signed URL `AttributeError` | Workload Identity provides token-only credentials; `generate_signed_url()` needs a private key | Bypass GCS for recordings: use HTTP POST upload (see Section 10a) |
| `InvalidImageName` on bot pod | `BOT_POD_IMAGE` included a tag AND `CUBER_RELEASE_VERSION` appended another, producing double-tag | `BOT_POD_IMAGE` must not include a tag; tag comes from `CUBER_RELEASE_VERSION` |
| Zombie bots stuck in stale states | Bots in `post_processing`/`leaving`/`joining` with no heartbeat, never cleaned up | Added `clean_up_bots_with_heartbeat_timeout_or_that_never_launched` to scheduler loop (see Section 10b) |
| `LAUNCH_BOT_METHOD` not in pod env | ConfigMap patched but pods not restarted | `kubectl rollout restart` all deployments after configmap/secret changes |

---

## 8. Bot Pod Lifecycle and Recording Reliability

### Problem

When a meeting ends, the bot pod receives a SIGTERM from Kubernetes. The bot's internal cleanup sequence (leave meeting, finalize recording, upload to GCS, process utterances) can take several minutes, but Kubernetes enforces a `terminationGracePeriodSeconds` timeout — after which the pod is forcefully killed (SIGKILL).

The default was **60 seconds**, which was not enough time for the GCS upload to complete, resulting in lost recordings.

### Fix: Increased Termination Grace Period

**File:** `bots/bot_pod_creator/bot_pod_creator.py`

Changed `termination_grace_period_seconds` from `60` to `300` (5 minutes) for both bot pods and webpage-streamer pods. This gives the bot enough time to:

1. Detect meeting end
2. Flush pending utterances
3. Finalize the recording file
4. Upload to GCS
5. Update bot state in the database

### Auto-Leave Settings

To ensure bots leave gracefully (before being killed), configure auto-leave when creating bots:

```bash
curl -X POST http://<IP>/api/v1/bots \
  -H "Authorization: Token <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_url": "...",
    "bot_name": "Neusis Bot",
    "automatic_leave": {
      "only_participant_in_meeting_timeout_seconds": 60,
      "silence_timeout_seconds": 600,
      "max_uptime_seconds": 7200
    }
  }'
```

| Setting | Default | Description |
|---|---|---|
| `only_participant_in_meeting_timeout_seconds` | 60 | Leave when bot is the only participant for this long |
| `silence_timeout_seconds` | 600 | Leave after continuous silence |
| `silence_activate_after_seconds` | 1200 | Delay before silence detection activates |
| `max_uptime_seconds` | None | Hard cap on bot lifetime |
| `waiting_room_timeout_seconds` | 900 | Leave if stuck in waiting room |
| `wait_for_host_to_start_meeting_timeout_seconds` | 600 | Leave if host never starts |

The most important setting for reliability is `only_participant_in_meeting_timeout_seconds` — it ensures the bot leaves cleanly when everyone else has left, triggering a proper upload before the pod terminates.

---

## 9. Current Architecture

```
Internet
    |
 Cloud NAT (attendee-nat)
    |
 GKE Autopilot (attendee-cluster, us-central1)
    |
    +-- attendee namespace
    |     +-- attendee-web (x2) ── Gunicorn, port 8000
    |     +-- attendee-worker (x2) ── Celery worker (orchestration only)
    |     +-- attendee-scheduler (x1) ── Scheduler daemon + stale bot cleanup
    |     +-- bot pods (dynamic) ── One per meeting (LAUNCH_BOT_METHOD=kubernetes)
    |
    +-- Cloud SQL (10.251.1.2:5432) ── PostgreSQL 15
    +-- Memorystore (10.251.0.4:6379) ── Redis 7.0
    +-- Artifact Registry ── Docker images
```

Recordings are uploaded via HTTP POST directly to the external bot-handler (see Section 10a), not stored in GCS.

**External IP:** `136.110.183.65` (LoadBalancer service)

---

## 10. Bot Launch Method: Kubernetes Pods

### Problem: OOMKilled Workers

Originally, bots ran as Celery tasks inside the `attendee-worker` pods. Each bot launches a headless Chrome instance for Teams (1-2 GB RAM). With `concurrency=4` and a 2Gi memory limit, a single bot would OOM the worker after ~3 minutes, killing all tasks on that pod.

GKE Autopilot also aggressively scales nodes, evicting worker pods and destroying bot browser sessions mid-meeting.

### Fix: Dedicated Bot Pods

Set `LAUNCH_BOT_METHOD=kubernetes` in the configmap. Now each bot gets its own isolated Kubernetes pod instead of running inside the worker.

```bash
kubectl patch configmap env -n attendee --type merge -p '{"data":{"LAUNCH_BOT_METHOD":"kubernetes"}}'
kubectl rollout restart deployment/attendee-worker -n attendee
```

**Bot pod resources (per bot):**

| Resource | Value | Configurable via |
|---|---|---|
| CPU | 4 cores | `BOT_CPU_REQUEST` |
| Memory | 4Gi | `BOT_MEMORY_REQUEST`, `BOT_MEMORY_LIMIT` |
| Ephemeral storage | 10Gi | `BOT_EPHEMERAL_STORAGE_REQUEST` |
| Restart policy | Never | Hardcoded |
| Termination grace | 300s | Hardcoded in `bot_pod_creator.py` |

**Maximum meeting duration by recording format (4Gi pod):**

| Format | Memory Growth | Estimated Max Duration |
|---|---|---|
| mp4 1080p | ~50-100 MB/min | 30-45 minutes |
| mp4 720p | ~30-60 MB/min | 45-60 minutes |
| **mp3 audio only** | ~5-10 MB/min | **3-4 hours** |
| none (captions only) | ~0 MB/min | Unlimited |

For longer meetings, increase `BOT_MEMORY_LIMIT` to `8Gi` or switch to `mp3` format.

### PodDisruptionBudget

**File:** `deploy/k8s/pdb.yaml` (new file)

Prevents GKE Autopilot from evicting all worker pods during node scale-down:

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: attendee-worker-pdb
  namespace: attendee
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: attendee
      component: worker
```

```bash
kubectl apply -f deploy/k8s/pdb.yaml
```

### Worker Memory

Workers no longer run bots (just orchestration), but the memory was increased to 4Gi as a safety margin in `deploy/k8s/deployments.yaml`.

---

## 10a. HTTP Recording Upload (Bypassing GCS)

### Problem

GCS signed URLs don't work with GKE Workload Identity. When a bot finishes recording and Attendee tries to generate a download URL via `generate_signed_url()`, it fails with:

```
AttributeError: you need a private key to sign credentials.
the credentials you are currently using <class 'google.auth.compute_engine.credentials.Credentials'> just contains a token
```

The recording file IS uploaded to GCS successfully, but clients can't download it via the API.

### Solution: HTTP POST to External Handler

Instead of storing recordings in GCS and serving signed URLs, bots POST the recording file directly to an external HTTP endpoint (e.g., `neusis-bot-scheduler`). This is controlled by the `RECORDING_UPLOAD_URL` environment variable.

**Set the env var:**

```bash
kubectl patch configmap env -n attendee --type merge \
  -p '{"data":{"RECORDING_UPLOAD_URL":"https://<your-bot-handler>/api/recordings/upload"}}'
kubectl rollout restart deployment/attendee-worker deployment/attendee-scheduler -n attendee
```

Bot pods pick this up automatically from the configmap since they are created after the change.

### How It Works

When `RECORDING_UPLOAD_URL` is set, the bot's cleanup sequence changes:

1. Bot finishes recording → writes mp3 to local filesystem
2. Instead of uploading to GCS, sends `POST` (multipart/form-data) to `RECORDING_UPLOAD_URL`
3. Payload includes: `file` (the mp3), `bot_id` (object_id), `bot_db_id`, `filename`, `meeting_url`
4. After successful upload, deletes local file
5. `recording_file_saved()` is NOT called (no GCS file reference in DB)

When `RECORDING_UPLOAD_URL` is **not** set, the original GCS upload flow is used.

### Files

- **`bots/bot_controller/http_file_uploader.py`** — New file. HTTP POST uploader with same interface as `GCSFileUploader` (`upload_file`, `wait_for_upload`, `delete_file`).
- **`bots/bot_controller/bot_controller.py`** — Modified `cleanup()` method to check for `RECORDING_UPLOAD_URL` and use `HTTPFileUploader` when set.

### HTTP POST Format

```
POST /api/recordings/upload HTTP/1.1
Content-Type: multipart/form-data

Fields:
  file: <binary mp3 data>  (field name: "file", filename: "bot_xxx-rec_yyy.mp3")
  bot_id: "bot_xxx"        (Attendee object_id)
  bot_db_id: "123"         (Attendee internal DB id)
  filename: "bot_xxx-rec_yyy.mp3"
  meeting_url: "https://teams.microsoft.com/l/meetup-join/..."  (if available)
```

Your receiving endpoint should return HTTP 2xx on success. The uploader has a 300-second timeout.

---

## 10b. Automatic Stale Bot Cleanup

### Problem

Bots can get stuck in stale states (`post_processing`, `leaving`, `joining`) if their pod crashes or loses connectivity. These zombie bots consume tracking resources and never resolve on their own.

### Solution

The scheduler daemon now runs the built-in `clean_up_bots_with_heartbeat_timeout_or_that_never_launched` management command every 60 seconds (every scheduler cycle).

**File:** `bots/management/commands/run_scheduler.py` — Added `_clean_up_stale_bots()` call to the main loop.

### What It Cleans Up

| Condition | Threshold | Action |
|---|---|---|
| Bot has no heartbeat for 10+ minutes | `HEARTBEAT_TIMEOUT_MINUTES` (default 10) | Terminates bot with `fatal_error` |
| Bot never launched (no heartbeat, 1+ hour old) | `NEVER_LAUNCHED_TIMEOUT_HOURS` (default 1) | Terminates bot with `fatal_error` |
| Orphaned K8s pod (no matching bot) | — | Deletes the pod |

For `LAUNCH_BOT_METHOD=kubernetes`, it also cleans up orphaned Kubernetes pods that no longer have a corresponding bot record.

### Manual Cleanup (if needed)

Force-terminate a stuck bot via Django shell:

```bash
kubectl exec -n attendee deployment/attendee-web -- python manage.py shell -c "
from bots.models import Bot, BotEventManager, BotEventTypes, BotEventSubTypes
bot = Bot.objects.get(object_id='bot_xxx')
BotEventManager.create_event(
    bot=bot,
    event_type=BotEventTypes.FATAL_ERROR,
    event_sub_type=BotEventSubTypes.FATAL_ERROR_PROCESS_TERMINATED,
)
print(f'Terminated {bot.object_id}, new state: {bot.state}')
"
```

---

## 11. External Bot Scheduler Integration (neusis-bot-scheduler)

### Overview

Bot creation is handled by an external Node.js service (`neusis-bot-scheduler`) running on Cloud Run, not by Attendee's internal scheduler. The internal `auto_create_bots_for_calendar_events` task was **removed** from the scheduler loop to avoid duplicate bots.

**Service URL:** `<cloud-run-url-bot-scheduler>`

### Flow

```
neusis-bot-scheduler                    Attendee
       │                                    │
       │── GET /api/v1/calendar_events ────>│  Read synced events
       │                                    │
       │── POST /api/v1/bots ──────────────>│  Create bot for meeting
       │                                    │
       │                              [Bot pod joins meeting]
       │                              [Bot records mp3]
       │                              [Meeting ends]
       │                              [HTTP POST mp3 to RECORDING_UPLOAD_URL]
       │                                    │
       │<─ POST /api/recordings/upload ─────│  Bot POSTs mp3 directly
       │                                    │
       │<── POST /api/webhooks/attendee ────│  Webhook: "post_processing_completed"
       │                                    │
       │── GET /api/v1/bots/{id}/transcript ─>│  Get transcript
       │                                    │
  [Analyze audio + transcript]              │
```

> **Note:** With HTTP recording upload enabled (`RECORDING_UPLOAD_URL`), recordings are POSTed directly to your handler. The recording download API (`GET /api/v1/bots/{id}/recording`) will NOT have a URL since the file is not stored in GCS. Your handler receives the mp3 file directly via the POST.

### API Endpoints Used by neusis-bot-scheduler

**Authentication header for all requests:** `Authorization: Token <API_KEY>`

#### List calendar events

```bash
curl http://136.110.183.65/api/v1/calendar_events \
  -H "Authorization: Token <API_KEY>"

# With filters:
curl "http://136.110.183.65/api/v1/calendar_events?start_time_gte=2026-03-11T00:00:00Z&calendar_id=cal_W9GpkJdy3boiTXu0" \
  -H "Authorization: Token <API_KEY>"
```

Returns events with `meeting_url`, `name`, `start_time`, `end_time`, `attendees`, and existing `bots` array for deduplication.

#### Create a bot

```bash
curl -X POST http://136.110.183.65/api/v1/bots \
  -H "Authorization: Token <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_url": "https://teams.microsoft.com/l/meetup-join/...",
    "bot_name": "Neusis Bot - Neusis AI",
    "recording_settings": {
      "format": "mp3"
    },
    "transcription_settings": {
      "meeting_closed_captions": {}
    },
    "automatic_leave_settings": {
      "only_participant_in_meeting_timeout_seconds": 60,
      "silence_timeout_seconds": 600
    }
  }'
```

#### Check bot status

```bash
curl http://136.110.183.65/api/v1/bots/<bot_id> \
  -H "Authorization: Token <API_KEY>"
```

#### Get recording download URL (after bot ends)

```bash
curl http://136.110.183.65/api/v1/bots/<bot_id>/recording \
  -H "Authorization: Token <API_KEY>"
```

Returns:
```json
{
  "url": "https://storage.googleapis.com/attendee-recordings-neusis-platform/bot_xxx-rec_yyy.mp3",
  "start_timestamp_ms": 1741660068582
}
```

#### Get transcript (after bot ends)

```bash
curl http://136.110.183.65/api/v1/bots/<bot_id>/transcript \
  -H "Authorization: Token <API_KEY>"
```

Returns:
```json
[
  {
    "speaker_name": "AK G",
    "speaker_uuid": "8:orgid:5a0fc3ce-...",
    "speaker_is_host": true,
    "timestamp_ms": 1773200873371,
    "duration_ms": 3256,
    "transcription": {
      "transcript": "Thank you for joining the call."
    }
  }
]
```

#### Trigger a calendar sync manually

```bash
# Via Django shell (no REST API endpoint for this)
kubectl exec -n attendee deployment/attendee-web -- python manage.py shell -c "
from bots.models import Calendar
from bots.tasks.sync_calendar_task import sync_calendar
cal = Calendar.objects.get(object_id='cal_W9GpkJdy3boiTXu0')
sync_calendar.delay(cal.id)
"
```

---

## 12. Webhooks

### Setup

A project-level webhook notifies `neusis-bot-scheduler` on every bot state change. Created via Django shell (no REST API endpoint for webhook subscription management):

```bash
kubectl exec -n attendee deployment/attendee-web -- python manage.py shell -c "
from bots.models import WebhookSubscription, WebhookTriggerTypes, Bot
project = Bot.objects.first().project
WebhookSubscription.objects.create(
    project=project,
    bot=None,
    url='<cloud-run-url-bot-scheduler>/api/webhooks/attendee',
    triggers=[WebhookTriggerTypes.BOT_STATE_CHANGE],
    is_active=True,
)
"
```

Alternatively, webhooks can be set per-bot inline when creating the bot via the API:

```bash
curl -X POST http://136.110.183.65/api/v1/bots \
  -H "Authorization: Token <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_url": "...",
    "bot_name": "Neusis Bot",
    "webhooks": [
      {
        "url": "<cloud-run-url-bot-scheduler>/api/webhooks/attendee",
        "triggers": ["bot.state_change"]
      }
    ]
  }'
```

### Webhook Payload

Attendee sends an HTTP POST to the registered URL on every bot state change:

```json
{
  "idempotency_key": "550e8400-e29b-41d4-a716-446655440000",
  "bot_id": "bot_dVV1WPJKof8Sr3TS",
  "bot_metadata": null,
  "trigger": "bot.state_change",
  "data": {
    "event_type": "post_processing_completed",
    "event_sub_type": null,
    "event_metadata": {}
  }
}
```

**Headers:**
```
Content-Type: application/json
User-Agent: Attendee-Webhook/1.0
X-Webhook-Signature: <base64 HMAC-SHA256 signature>
```

The signature is computed using a project webhook secret stored in the database. Verify with:

```python
import hmac, hashlib, base64, json
payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
signature = base64.b64encode(
    hmac.new(secret_bytes, payload_json.encode("utf-8"), hashlib.sha256).digest()
).decode("utf-8")
```

### Key Event Types

| `data.event_type` | Meaning | Action |
|---|---|---|
| `join_requested` | Bot is starting to join | — |
| `joined_waiting_room` | Bot is in Teams lobby | — |
| `joined_meeting` | Bot joined, recording started | — |
| `left_meeting` | Bot left, upload in progress | — |
| **`post_processing_completed`** | **Recording + transcript ready** | **Fetch recording and transcript** |
| `fatal_error` | Bot failed | Handle error |

### Retry Behavior

- 3 retries with exponential backoff
- Non-2xx response triggers retry
- After 3 failures, delivery is marked as failed

---

## 13. Transcription

### Current Setup: Teams Built-in Captions

Transcription uses **Microsoft Teams native closed captions** — no external API (Deepgram, etc.) needed.

**How it works:**
1. When the bot joins a Teams meeting, it enables closed captions via the Teams UI
2. Teams generates captions in real-time using its own speech recognition
3. The bot captures each caption as an `Utterance` with speaker name, timestamp, and text
4. Utterances are stored in the database and exposed via the transcript API

**Configuration:** Set `transcription_settings` when creating the bot:

```json
{
  "transcription_settings": {
    "meeting_closed_captions": {}
  }
}
```

This is the default for Teams meetings. No API keys or external services required.

### Transcript API

```bash
curl http://136.110.183.65/api/v1/bots/<bot_id>/transcript \
  -H "Authorization: Token <API_KEY>"
```

Response:
```json
[
  {
    "speaker_name": "AK G",
    "speaker_uuid": "8:orgid:5a0fc3ce-434e-4a81-9211-44a783d74d47",
    "speaker_user_uuid": null,
    "speaker_is_host": true,
    "timestamp_ms": 1773200873371,
    "duration_ms": 3256,
    "transcription": {
      "transcript": "Thank you for joining the call."
    }
  },
  {
    "speaker_name": "John Doe",
    "speaker_uuid": "8:orgid:...",
    "speaker_is_host": false,
    "timestamp_ms": 1773200878561,
    "duration_ms": 3367,
    "transcription": {
      "transcript": "Good morning everyone."
    }
  }
]
```

### Alternative Transcription Providers

If Teams captions quality is insufficient, Attendee supports these providers (configured via `transcription_settings` on bot creation):

| Provider | Setting | Requires |
|---|---|---|
| **Teams Captions** (current) | `{"meeting_closed_captions": {}}` | Nothing |
| Deepgram | `{"deepgram": {"api_key": "..."}}` | Deepgram API key |
| OpenAI Whisper | `{"openai": {"api_key": "..."}}` | OpenAI API key |
| Assembly AI | `{"assembly_ai": {"api_key": "..."}}` | AssemblyAI API key |
| Gladia | `{"gladia": {"api_key": "..."}}` | Gladia API key |

Teams captions are free, speaker-attributed, and work well for clear speech. Switch to Deepgram or Whisper if you need better accuracy for noisy audio or non-English languages.

---

## 14. Current Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  neusis-bot-scheduler (Cloud Run, external)                     │
│  - Reads calendar events: GET /api/v1/calendar_events           │
│  - Creates bots:          POST /api/v1/bots                     │
│  - Receives webhooks:     POST /api/webhooks/attendee           │
│  - Receives recordings:   POST /api/recordings/upload (HTTP)    │
│  - Gets transcripts:      GET /api/v1/bots/{id}/transcript      │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTPS
                            ▼
┌─────────────────── GKE Autopilot (attendee namespace) ──────────┐
│                                                                  │
│  attendee-web (2 replicas, 2Gi)                                 │
│  └─ Django/Gunicorn — REST API + admin                          │
│                                                                  │
│  attendee-scheduler (1 replica, 512Mi)                          │
│  └─ Polls every 60s:                                            │
│     • Launches scheduled bots (creates K8s pods)                │
│     • Triggers calendar syncs (every 24h)                       │
│     • Refreshes OAuth tokens                                    │
│     • Cleans up stale/zombie bots (heartbeat timeout)           │
│                                                                  │
│  attendee-worker (2 replicas, 4Gi)                              │
│  └─ Celery workers — lightweight task processing:               │
│     • sync_calendar (Microsoft Graph API)                       │
│     • launch_scheduled_bot → creates bot K8s pod                │
│     • deliver_webhook                                           │
│     • process_utterance (stores captions)                       │
│                                                                  │
│  bot-{id}-{hash} (one pod PER bot, 4Gi + 4 CPU)                │
│  └─ Headless Chrome → joins Teams meeting                       │
│     • Records audio via ffmpeg (mp3)                            │
│     • Captures Teams closed captions                            │
│     • POSTs recording to RECORDING_UPLOAD_URL on exit           │
│     • Pod dies after meeting (restartPolicy: Never)             │
│                                                                  │
│  Cloud SQL ──── PostgreSQL 15 (10.251.1.2)                      │
│  Memorystore ── Redis (10.251.0.4) — Celery broker              │
│  Artifact Registry ── Docker images                              │
└──────────────────────────────────────────────────────────────────┘
```

**External IP:** `136.110.183.65` (GKE Ingress LoadBalancer)

---

## 15. Remaining Items

- **SSL/Domain:** No HTTPS configured. `SITE_DOMAIN: "*"` prevents Microsoft Graph webhook subscriptions (push notifications). Calendar sync relies on polling (every 24h). Set up a domain + cert to enable real-time push sync.
- **Calendar sync frequency:** Currently every 24 hours via scheduler. New meetings created less than 24h before start time may not be synced in time. Workaround: trigger manual sync via Django shell (see Section 11).
- **Email backend:** Using `django.core.mail.backends.console.EmailBackend` via configmap.
- **GCS recording download:** With HTTP upload enabled, the `GET /api/v1/bots/{id}/recording` endpoint returns no URL. If you need the GCS download flow, you must either (a) use a JSON service account key instead of Workload Identity, or (b) set up a signing service account with `iam.serviceAccounts.signBlob` permission.
- **Git push:** Changes are on local `akg-dev` branch.
