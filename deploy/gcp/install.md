# Deploy Attendee to GCP — Production Runbook

**Tracks:** [Neusis-AI-Org/attendee#1](https://github.com/Neusis-AI-Org/attendee/issues/1)  •  **Last updated:** 2026-05-15

## ⚠ Topology change (2026-05-15)

This runbook **no longer provisions a standalone VM for Attendee**. The deploy plan changed during local smoke iteration: Attendee now runs on the **same Compute Engine VM as project-brain**, sharing one Postgres (separate databases) and one Redis (separate DB numbers). This halves VM cost (~$48/mo saved) and simplifies ops.

**Follow the project-brain runbook instead** — Attendee install is **Part 2** (Steps 15–19) of that runbook:

👉 **[project-brain/deploy/gcp/install.md](https://github.com/Neusis-AI-Org/project-brain/blob/neusis/meeting-bot-integration/deploy/gcp/install.md#part-2--add-attendee-onto-the-same-vm)**

Quick reference for what Part 2 of that runbook does:

| Step | What it does |
|---|---|
| Step 15 | Add `attendee.neusis.ai` A record at Spaceship (same VM IP as `brain.neusis.ai`) |
| Step 16 | `git clone` this repo to `/opt/attendee` on the project-brain VM |
| Step 17 | Create `attendee_production` Postgres DB + `attendee` role on the shared Postgres |
| Step 18 | Write `.env.attendee` + `docker-compose.attendee.yml` overlay + append the Attendee block to project-brain's Caddyfile + `docker compose up -d --build` |
| Step 19 | Migrate Django, create superuser, generate API token, stash in Secret Manager |

## What still lives in this repo

The artifacts referenced by project-brain's Part 2 — these stay in this repo and are sourced into the deployed VM via `git clone`:

| File | Purpose |
|---|---|
| `Dockerfile` | Image built by `docker compose ... up -d --build` from project-brain's compose overlay |
| `deploy/gcp/Caddyfile` | Reference Caddy snippet used in the project-brain Caddyfile (only the `attendee.neusis.ai { ... }` block — paste into project-brain's `Caddyfile` per Part 2 Step 18) |
| `deploy/gcp/recording-cleanup.sh` | Daily cron script mounted by the `attendee-recording-cleanup` service in project-brain's overlay |
| `deploy/gcp/docker-compose.gcp.yaml` | **Legacy** — was the standalone-VM compose. Kept for reference / future split. Not used by the consolidated deploy. |

## Why the standalone runbook was retired

The trade-off was explicitly chosen: cost savings + ops simplicity over resource isolation. The compose overlay pattern in project-brain Step 18 is designed so splitting Attendee back onto its own VM later is mechanical (`mv` the overlay + `.env.attendee` + `attendee_recordings` volume to a fresh box). If/when concurrent meeting load justifies dedicated hardware, the legacy `deploy/gcp/docker-compose.gcp.yaml` here is the starting point.

See [`notetaker-deployment-prd.md` §5.1](https://github.com/Neusis-AI-Org/project-brain/blob/neusis/meeting-bot-integration/docs/notetaker-deployment-prd.md#51-topology-in-scope-for-v1) for the full topology + cost analysis.
