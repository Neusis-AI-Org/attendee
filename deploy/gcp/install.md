# Deploy Attendee to GCP — Production Runbook

**Tracks:** [Issue #1](https://github.com/Neusis-AI-Org/attendee/issues/1)  •  **Last updated:** 2026-05-15

This runbook deploys Attendee to a single Compute Engine VM on GCP, fronted by Caddy with auto-issued Let's Encrypt TLS, with web + worker (Celery, `--concurrency=1`) + scheduler + Postgres + Redis + a daily recording-cleanup cron all co-located via Docker Compose.

> **⚠️ Read first**
> Sections marked **HITL (manual)** require the operator to act in a console (GCP, Microsoft Entra Admin Center, etc.). Sections marked **shell** are copy-paste-ready commands. Run them top-to-bottom.

---

## Prerequisites (one-time, before you start)

- A GCP project with billing enabled. We use **`neusis-platform`**.
- A domain you control where you can add an A record. We use **`neusis.ai`** (DNS at Spaceship).
- `gcloud` CLI installed and authenticated.
- Local clone of `Neusis-AI-Org/attendee` on branch **`akg-dev`** (carries V1 deployment work).
- A Microsoft account with the right to create OAuth applications in the M365 tenant the bot will join meetings in (only required for Teams platform support; details in Step 9).

### Decisions baked into the runbook

| Variable | Value |
|---|---|
| `GCP_PROJECT` | `neusis-platform` |
| `REGION` | `us-central1` |
| `ZONE` | `us-central1-a` |
| `APP_DOMAIN` | `attendee.neusis.ai` |
| `VM_NAME` | `attendee-prod` |
| `EXTERNAL_IP` (after step 1) | _(captured from `gcloud compute instances describe`)_ |

Set these in your shell now:

```bash
export GCP_PROJECT=neusis-platform
export REGION=us-central1
export ZONE=us-central1-a
export APP_DOMAIN=attendee.neusis.ai
export VM_NAME=attendee-prod
gcloud config set project "$GCP_PROJECT"
```

---

## Step 1 — Provision the VM (shell)

The project-brain runbook (`deploy/gcp/install.md` in that repo) already enabled `compute.googleapis.com` and `iap.googleapis.com` — no API enable needed here.

```bash
gcloud compute instances create "$VM_NAME" \
  --project="$GCP_PROJECT" \
  --zone="$ZONE" \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB \
  --boot-disk-type=pd-ssd \
  --tags=caddy \
  --metadata=enable-oslogin=TRUE \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring

export EXTERNAL_IP="$(gcloud compute instances describe "$VM_NAME" \
  --zone="$ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
echo "EXTERNAL_IP=$EXTERNAL_IP"
```

> **Firewall rules:** `allow-https-public`, `allow-http-public`, and `allow-ssh-iap` were created during the project-brain deploy and target the `caddy` tag (which this VM has). No firewall changes needed here.

---

## Step 2 — DNS A record at Spaceship (HITL)

In the Spaceship dashboard:

1. Sign in at https://www.spaceship.com.
2. Go to **Domains** → **neusis.ai** → **DNS records**.
3. Add:
   - Type: **A**
   - Host: **`attendee`**
   - Value: the `$EXTERNAL_IP` from Step 1
   - TTL: 300

Verify from your local machine (1–5 min for propagation):

```bash
dig +short attendee.neusis.ai
# Expect: $EXTERNAL_IP
```

Don't proceed past Step 7 (compose up) until DNS resolves correctly — otherwise Caddy will fail Let's Encrypt's HTTP-01 challenge and may rate-limit you.

---

## Step 3 — SSH in and install Docker (shell)

```bash
gcloud compute ssh "$VM_NAME" \
  --project="$GCP_PROJECT" \
  --zone="$ZONE" \
  --tunnel-through-iap
```

On the VM:

```bash
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -yqq ca-certificates curl gnupg openssl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -yqq \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
# Re-login or `newgrp docker` to pick up the group change
```

---

## Step 4 — Get the repo onto the VM (shell, on VM)

The repo is private and the org disables deploy keys. Easiest path: download the tarball via the gh API on your local machine, then `scp` it to the VM. From your **local** machine:

```bash
gh api -H "Accept: application/vnd.github.tarball" \
  /repos/Neusis-AI-Org/attendee/tarball/akg-dev > /tmp/attendee.tgz
gcloud compute scp --tunnel-through-iap \
  --project=neusis-platform --zone=us-central1-a \
  /tmp/attendee.tgz attendee-prod:/tmp/attendee.tgz
```

Back on the VM:

```bash
sudo mkdir -p /opt/attendee
sudo chown "$USER:$USER" /opt/attendee
cd /opt/attendee
tar xzf /tmp/attendee.tgz --strip-components=1
rm /tmp/attendee.tgz
```

For routine updates after the first deploy, repeat this `gh api ... | scp` step from local — it's faster than setting up GitHub auth on the VM.

---

## Step 5 — Generate `.env` (shell, on VM)

```bash
cd /opt/attendee
cat > .env <<EOF
# ── Domain + ACME ─────────────────────────────────────────────
APP_DOMAIN=attendee.neusis.ai
ACME_EMAIL=ops@neusis.ai

# ── Database ───────────────────────────────────────────────────
POSTGRES_DB=attendee_production
POSTGRES_USER=attendee
POSTGRES_PASSWORD=$(openssl rand -base64 32 | tr -d '=+/' | head -c 32)
DATABASE_URL=postgres://attendee:\${POSTGRES_PASSWORD}@postgres:5432/attendee_production
POSTGRES_SSL_REQUIRE=false

# ── Django ─────────────────────────────────────────────────────
DJANGO_SETTINGS_MODULE=attendee.settings.production
DJANGO_SECRET_KEY=$(openssl rand -base64 64 | tr -d '=+/' | head -c 50)
ALLOWED_HOSTS=attendee.neusis.ai
SITE_DOMAIN=attendee.neusis.ai
CSRF_TRUSTED_ORIGINS=https://attendee.neusis.ai

# Caddy fronts TLS — leave Django's redirect-to-https off and trust the
# X-Forwarded-Proto header Caddy sets.
DJANGO_SSL_REQUIRE=false
DISABLE_EMAIL=true

# ── Recording retention (consumed by recording-cleanup cron) ───
RECORDING_RETENTION_DAYS=5

# ── Storage ─────────────────────────────────────────────────────
# Local filesystem only for V1; no GCS, no S3.
STORAGE_PROTOCOL=filesystem

# ── Bot launch method ──────────────────────────────────────────
# Default (unset) = launch bot via the celery worker process. Pinned to
# concurrency=1 in docker-compose.gcp.yaml. Bump to docker-compose-multi-host
# if you ever scale to multiple bot launcher VMs.
EOF

chmod 600 .env
```

> The double-`$` escaping above is intentional — bash expands `$(openssl ...)` immediately when generating the file, but escapes `\${POSTGRES_PASSWORD}` so it's a literal `${POSTGRES_PASSWORD}` placeholder Compose can interpolate at runtime.

---

## Step 6 — Build and bring up the stack (shell, on VM)

```bash
cd /opt/attendee
docker compose -f deploy/gcp/docker-compose.gcp.yaml --env-file .env up -d --build
docker compose -f deploy/gcp/docker-compose.gcp.yaml ps
```

Expect 6 services running: `attendee-app`, `attendee-worker`, `attendee-scheduler`, `postgres`, `redis`, `caddy`, `recording-cleanup`.

Caddy needs ~30–60s to issue the Let's Encrypt cert on first start. Watch logs:

```bash
docker compose -f deploy/gcp/docker-compose.gcp.yaml logs -f caddy
# Wait for: certificate obtained successfully
```

---

## Step 7 — Run Django migrations + create superuser (shell, on VM)

```bash
cd /opt/attendee
docker compose -f deploy/gcp/docker-compose.gcp.yaml exec attendee-app \
  python manage.py migrate

# Create superuser non-interactively. Capture the password securely.
SUPERUSER_PASSWORD="$(openssl rand -base64 24 | tr -d '=+/' | head -c 24)"
echo "SUPERUSER_PASSWORD: $SUPERUSER_PASSWORD"
docker compose -f deploy/gcp/docker-compose.gcp.yaml exec -T \
  -e DJANGO_SUPERUSER_PASSWORD="$SUPERUSER_PASSWORD" \
  -e DJANGO_SUPERUSER_EMAIL=admin@neusis.ai \
  -e DJANGO_SUPERUSER_USERNAME=admin \
  attendee-app python manage.py createsuperuser --noinput
```

Save the password somewhere safe (Secret Manager, password manager).

---

## Step 8 — Verify HTTPS + generate API token (HITL via SSH port-forward)

The Django admin UI is bound to localhost only by Caddy (it returns 404 on the public path `/admin`). Reach it via SSH port-forward from your **local** machine:

```bash
gcloud compute ssh attendee-prod \
  --project=neusis-platform --zone=us-central1-a \
  --tunnel-through-iap -- -L 8000:127.0.0.1:8000
```

Then in your local browser, open `http://localhost:8000/admin`. Sign in with `admin@neusis.ai` and the password from Step 7.

In the admin UI:

1. Navigate to **Bots > API keys** (exact path varies by Attendee version — look for "API keys" or "Tokens").
2. Generate a new API key. Copy it. This is the `ATTENDEE_API_TOKEN` that bot-service (Issue #3) and any `curl` smoke tests will use.
3. Stash in GCP Secret Manager from your local machine:
   ```bash
   echo -n "<paste API key>" | \
     gcloud secrets create attendee-api-token --data-file=- --project=neusis-platform
   ```

---

## Step 9 — Configure platform-specific OAuth (HITL — Microsoft Teams in V1)

Attendee needs a Microsoft Entra OIDC app to join Teams meetings. **This is a separate app from the project-brain SSO app you created in `project-brain/deploy/gcp/install.md` Step 5** — different scope and different redirect URIs.

1. https://entra.microsoft.com → **App registrations** → **New registration**.
2. Name: `Attendee — Teams meeting bot`.
3. Supported account types: per your tenant policy.
4. Redirect URI (Web): `https://attendee.neusis.ai/projects/teams_credentials/oauth/callback` (verify exact path against current Attendee docs — the Teams OAuth callback path may have changed).
5. **API permissions**: Microsoft Graph → Delegated → `OnlineMeetings.ReadWrite`, `User.Read`. Some Teams bot patterns require Application permissions (Calls.JoinGroupCall.All etc.) — see Attendee's Teams setup docs for the canonical list.
6. **Certificates & secrets** → New client secret. Copy the value.
7. In the Attendee admin UI (via the SSH port-forward from Step 8), navigate to the Teams Credentials section and paste the client_id, tenant_id, and client_secret.

---

## Step 10 — Smoke test (HITL — needs a real Teams meeting)

From your **local** machine, with the API token from Step 8:

```bash
ATTENDEE_API_TOKEN="$(gcloud secrets versions access latest \
  --secret=attendee-api-token --project=neusis-platform)"

# Create a real Teams meeting on a calendar you control. Copy the join URL.
TEAMS_URL="<paste>"

curl -X POST "https://attendee.neusis.ai/api/v1/bots" \
  -H "Authorization: Token $ATTENDEE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"meeting_url\": \"$TEAMS_URL\", \"bot_name\": \"Notetaker (smoke test)\"}"
```

Verify on the VM:

```bash
docker compose -f deploy/gcp/docker-compose.gcp.yaml logs -f attendee-worker
# Expect: bot launches, Chrome starts, joins the meeting
```

The bot should appear in Teams within ~30s. Let it record for at least 30 minutes to validate the `parec | lame` audio pipeline (recent fix — supersedes the old ffmpeg ~7.5 min cap). Verify the recording file lands in the `recordings` Docker volume:

```bash
docker compose -f deploy/gcp/docker-compose.gcp.yaml exec attendee-worker ls -la /recordings
```

---

## Step 11 — Verify retention cron (shell, on VM)

```bash
# Touch a fake file with a 6-day-old mtime
docker compose -f deploy/gcp/docker-compose.gcp.yaml exec attendee-worker \
  sh -c 'touch -d "6 days ago" /recordings/old-test.mp3 && ls -la /recordings/old-test.mp3'

# Manually trigger the cleanup script (cron normally fires at 03:00 UTC)
docker compose -f deploy/gcp/docker-compose.gcp.yaml exec recording-cleanup \
  /usr/local/bin/recording-cleanup.sh

# Confirm the old file was deleted
docker compose -f deploy/gcp/docker-compose.gcp.yaml exec attendee-worker \
  ls -la /recordings/old-test.mp3
# Expect: No such file or directory
```

---

## Acceptance criteria checklist (from [Issue #1](https://github.com/Neusis-AI-Org/attendee/issues/1))

- [ ] `https://attendee.neusis.ai` serves the Attendee API over a valid Let's Encrypt cert.
- [ ] `GET /api/v1/health` (or equivalent — verify against current Attendee version) returns 200.
- [ ] A `curl POST /api/v1/bots` against the live API successfully launches a bot into a real Teams meeting.
- [ ] Bot records ≥30 minutes of continuous audio without truncation.
- [ ] Recording artifacts land at `/recordings/<bot_id>.mp3` (or whatever naming scheme the current Attendee version uses).
- [ ] Recording cleanup cron deleted a 6-day-old test file.
- [ ] Django admin UI is **not** reachable from the public internet (`curl https://attendee.neusis.ai/admin` returns 404).
- [ ] This runbook merged.

---

## Troubleshooting

**Caddy can't issue the cert.** DNS hasn't propagated. `dig +short attendee.neusis.ai` from a third-party network. If it doesn't return your VM IP, wait. Second cause: port 80 is firewall-blocked — confirm `allow-http-public` exists and targets the `caddy` tag.

**Bot can't join Teams meetings.** Most common: Teams credentials not configured (Step 9), or the Microsoft app needs Application permissions + admin consent. Inspect `attendee-worker` logs.

**`docker compose ... up -d --build` is slow.** First build downloads the Ubuntu base + ~2 GB of ML/Chrome dependencies. Subsequent builds use cache. Allow ~10 minutes for the first build.

**Recording is cut short / silent.** Check that `parec` / `lame` are running inside the worker container (`docker compose ... exec attendee-worker pidof parec lame`). The fix for the ffmpeg ~7.5 min cap is in `entrypoint.sh` — make sure you're on the latest `akg-dev` (or main) branch.

**Out-of-memory during a 3hr meeting.** e2-standard-2 has 8 GB RAM. If you hit OOM with one bot, bump to e2-standard-4 (`gcloud compute instances set-machine-type ...`). PRD §9 risks called this out.

---

## What's next

When this runbook is fully green:

- [ ] Mark Issue #1 acceptance criteria as ticked.
- [ ] Hand off to **bot-service deploy** (`Neusis-AI-Org/neusis-meeting-bot#1`) which consumes the `attendee-api-token` from Step 8 and `https://attendee.neusis.ai` as `ATTENDEE_BASE_URL`.
- [ ] Run the **end-to-end smoke** (`Neusis-AI-Org/project-brain#80`) once project-brain, Attendee, and bot-service are all up.
