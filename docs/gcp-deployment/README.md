# GCP Deployment Documentation

Engineering reference for the Neusis GKE deployment of Attendee. These docs are fork-only — they do not exist on upstream `main`.

| Doc | Purpose |
|---|---|
| [deployment-guide.md](deployment-guide.md) | End-to-end GCP deployment runbook (Terraform, GKE, Cloud SQL, Memorystore, Cloud NAT, Workload Identity, native GCS storage backend, Microsoft calendar integration, bot pod lifecycle, recording upload paths). Start here for a fresh deployment. |
| [bot-flow-architecture.md](bot-flow-architecture.md) | Block diagrams (Mermaid) for the bot lifecycle across `attendee` (GKE) and `neusis-bot-scheduler` (Cloud Run). Read this first to understand how the two services interact. |
| [bot-capacity-and-cross-tenant-access.md](bot-capacity-and-cross-tenant-access.md) | Operational guide for scaling concurrent bots, GKE Autopilot quota tuning, and the five mitigation options for cross-tenant Teams meeting access (federation, guest invite, anonymous join, dedicated bot account, per-meeting lobby bypass). |
| [audio-truncation-fix.md](audio-truncation-fix.md) | Investigation and resolution of the ffmpeg ~10 min audio truncation bug. Captures the diagnostic approach, dead ends explored, root cause, and the `parec | lame` fix. Read this if recordings ever truncate again — the AUDIO_DIAG tripwire it documents will localize the regression instantly. |
