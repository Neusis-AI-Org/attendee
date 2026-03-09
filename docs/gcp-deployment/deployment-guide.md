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
BOT_POD_IMAGE: "us-central1-docker.pkg.dev/neusis-platform/attendee/attendee"
BOT_POD_SERVICE_ACCOUNT_NAME: "attendee-bot"
DJANGO_SETTINGS_MODULE: "attendee.settings.production-gke"
```

### `deploy/k8s/secret.yaml` (gitignored)

Contains sensitive values: `DATABASE_URL`, `REDIS_URL`, `DJANGO_SECRET_KEY`, `CREDENTIALS_ENCRYPTION_KEY`, `CUBER_RELEASE_VERSION`.

### `deploy/k8s/deployments.yaml`

Three deployments in namespace `attendee`:

| Deployment | Replicas | Purpose |
|---|---|---|
| `attendee-web` | 2 | Gunicorn (Django API server) with health probes |
| `attendee-worker` | 2 | Celery worker (4 concurrency) for async tasks |
| `attendee-scheduler` | 1 | Celery beat scheduler |

All use `serviceAccountName: attendee-app` with Workload Identity.

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

---

## 8. Current Architecture

```
Internet
    |
 Cloud NAT (attendee-nat)
    |
 GKE Autopilot (attendee-cluster, us-central1)
    |
    +-- attendee namespace
    |     +-- attendee-web (x2) ── Gunicorn, port 8000
    |     +-- attendee-worker (x2) ── Celery worker
    |     +-- attendee-scheduler (x1) ── Celery beat
    |     +-- bot pods (dynamic) ── Created per meeting
    |
    +-- Cloud SQL (10.251.1.2:5432) ── PostgreSQL 15
    +-- Memorystore (10.251.0.4:6379) ── Redis 7.0
    +-- GCS (attendee-recordings-neusis-platform) ── Recordings
    +-- Artifact Registry ── Docker images
```

**External IP:** `136.110.183.65` (LoadBalancer service)

---

## 9. Remaining Items

- **SSL/Domain:** No HTTPS configured yet. `SITE_DOMAIN: "*"` prevents Microsoft webhook subscriptions from working. Set up a domain + cert to enable real-time calendar push notifications.
- **Email backend:** `production-gke.py` still defaults to SMTP. Using `django.core.mail.backends.console.EmailBackend` via configmap override.
- **Auto bot creation:** Calendar events are synced but bots are not auto-created. Need a webhook handler or script to create bots for events with meeting URLs.
