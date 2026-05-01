# Bot Capacity, Scaling, and Cross-Tenant Access

Operational guide for bot resource management, scaling concurrent bots, and joining meetings hosted on external Microsoft Teams tenants.

---

## 1. Bot Pod Resources

Each bot runs as an isolated Kubernetes pod with the following defaults:

| Resource | Default | Env Var |
|---|---|---|
| CPU | 4 cores | `BOT_CPU_REQUEST` |
| Memory | 4Gi | `BOT_MEMORY_REQUEST`, `BOT_MEMORY_LIMIT` |
| Ephemeral storage | 10Gi | `BOT_EPHEMERAL_STORAGE_REQUEST` |
| Termination grace | 300s | Hardcoded in `bot_pod_creator.py` |
| Restart policy | Never | Hardcoded |

For **mp3 audio-only** recording, 4 CPU cores is overkill. Recommended settings for audio-only:

```bash
kubectl patch configmap env -n attendee --type merge -p '{
  "data": {
    "BOT_CPU_REQUEST": "1",
    "BOT_MEMORY_REQUEST": "2Gi",
    "BOT_MEMORY_LIMIT": "2Gi",
    "BOT_EPHEMERAL_STORAGE_REQUEST": "5Gi"
  }
}'
```

---

## 2. Concurrent Bot Capacity

GKE Autopilot provisions nodes on demand. The bottleneck is CPU quota.

### Current capacity (default 24 vCPU quota)

| Component | CPU Usage |
|---|---|
| attendee-web (2 replicas) | 2 CPU |
| attendee-worker (2 replicas) | 2 CPU |
| attendee-scheduler (1 replica) | 0.25 CPU |
| **Available for bots** | **~19.75 CPU** |

| Bot CPU Setting | Concurrent Bots | Best For |
|---|---|---|
| 4 CPU (default) | ~4-5 | Video recording (mp4) |
| 1 CPU (recommended) | ~16-20 | Audio-only recording (mp3) |

### Scaling beyond defaults

**Step 1 — Reduce bot CPU** (immediate, no GCP changes):

```bash
kubectl patch configmap env -n attendee --type merge -p '{"data":{"BOT_CPU_REQUEST":"1"}}'
```

**Step 2 — Request higher Autopilot CPU quota:**

```bash
# Check current quota
gcloud container clusters describe attendee-cluster --region us-central1 \
  --format="value(autopilot.workloadPolicyConfig)"
```

Request increase: GCP Console → IAM & Admin → Quotas → Filter "CPUs" → Select `us-central1` → Request increase.

| Quota | Bots at 1 CPU | Bots at 4 CPU |
|---|---|---|
| 24 vCPU (default) | ~20 | ~5 |
| 48 vCPU | ~44 | ~11 |
| 100 vCPU | ~96 | ~24 |

**Step 3 — Switch to GKE Standard** (for 100+ bots):

- Define node pools with specific machine types (e.g., `e2-standard-8`)
- Use cluster autoscaler with min/max node counts
- More cost-effective at scale

---

## 3. Always-On Services

These must run 24/7:

| Service | Purpose | What breaks if down |
|---|---|---|
| `attendee-web` | REST API (bot creation, transcripts) | neusis-bot-scheduler can't create bots or fetch results |
| `attendee-worker` | Celery tasks (calendar sync, bot launch, webhooks) | Calendar won't sync, bots won't launch, webhooks won't deliver |
| `attendee-scheduler` | Scheduler daemon (60s polling loop) | Scheduled bots won't launch, stale bots won't be cleaned up |

Bot pods are **ephemeral** — they spin up per-meeting and terminate after. No always-on cost.

---

## 4. Cross-Tenant Teams Access

### The Problem

When a meeting is hosted on an **external Teams tenant** (e.g., `taodigitalsolutions.com`), the tenant's security policies may block the bot from joining. The bot joins via a headless Chrome browser, and Teams treats it as either:

- **Anonymous user** (`teamsvisitor`) — if the bot doesn't sign in
- **External authenticated user** — if the bot signs in with a Microsoft account from a different tenant

Many enterprise tenants block one or both of these by default.

### Error Symptoms

Bot logs will show:

```
Sign in required. Raising UiLoginRequiredException
Meeting requires login, but Teams bot login credentials are not available
```

Or, if login credentials are configured:

```
Login completed, redirecting to meeting page
...
Sign in required. Raising UiLoginRequiredException
Meeting requires login, but we already tried to login, so we can't retry
```

The second case means the bot authenticated successfully but the **target tenant still rejected** the external user.

### How Teams Meeting Access Works

Teams meeting access is controlled at multiple levels:

```
Tenant Admin Policy (highest priority)
  └─ Anonymous users can join meetings: On/Off
  └─ External access: Allow/Block specific domains
  └─ Guest access: On/Off
      └─ Meeting Organizer Settings
          └─ Who can bypass lobby: Everyone / People in my org / ...
          └─ Who can present: Everyone / People in my org / ...
              └─ Per-Meeting Options (set by organizer for individual meetings)
```

If the **tenant admin** has disabled anonymous or external access, per-meeting settings cannot override it.

---

## 5. Solutions for Cross-Tenant Access

### Option A: External Access (Federation) — Recommended

**What it does:** Allows users from `neusis.ai` to join meetings on the target tenant as recognized external users.

**Who does it:** Target tenant admin (e.g., TAO admin).

**Effort:** One-time, 2-minute admin change.

**Steps for the target tenant admin:**

1. Sign in to [Microsoft Teams Admin Center](https://admin.teams.microsoft.com)
2. Navigate to **Users → External access**
3. Under "Choose which external domains your users have access to", select one of:
   - **Allow all external domains** (simplest, allows any domain)
   - **Allow only specific external domains** → Click **Allow domains** → Add `neusis.ai`
4. Click **Save**
5. Wait 15-30 minutes for the policy to propagate

**On the Neusis side** (your side), also ensure `neusis.ai` allows outbound federation:

1. Sign in to [Microsoft Teams Admin Center](https://admin.teams.microsoft.com) with your Neusis admin account
2. Navigate to **Users → External access**
3. Ensure external access is **not blocked** (either "Allow all" or explicitly allow the target domain)

**Additionally**, configure bot login credentials in Attendee so the bot authenticates as a Neusis user:

```bash
# Store credentials (one-time setup)
kubectl exec -n attendee deployment/attendee-web -- python manage.py shell -c "
from bots.models import Bot, Credentials
project = Bot.objects.first().project
creds, created = Credentials.objects.get_or_create(
    project=project,
    credential_type=Credentials.CredentialTypes.TEAMS_BOT_LOGIN,
)
creds.set_credentials({
    'username': 'bot-neusis@neusis.ai',
    'password': '<password>'
})
creds.save()
print('Saved')
"
```

Then ensure bots are created with login enabled:

```json
{
  "meeting_url": "...",
  "bot_name": "Neusis Bot",
  "teams_settings": {
    "use_login": true,
    "login_mode": "only_if_required"
  }
}
```

The `only_if_required` mode is recommended — the bot first tries anonymous join (faster), and only signs in if the meeting requires it.

---

### Option B: Guest Access

**What it does:** The bot's Microsoft account is added as a **guest user** in the target tenant's Azure AD/Entra directory. The bot is then a recognized member (guest) of that tenant.

**Who does it:** Target tenant admin.

**Effort:** One-time per tenant. Requires the bot to accept an invitation.

**Steps for the target tenant admin:**

1. Sign in to [Microsoft Entra Admin Center](https://entra.microsoft.com)
2. Navigate to **Users → All users → Invite external user**
3. Enter the bot's email: `bot-neusis@neusis.ai`
4. Add a personal message (optional): "Bot account for meeting transcription"
5. Click **Invite**
6. The bot email receives an invitation

**On the Neusis side:**

1. Accept the guest invitation sent to `bot-neusis@neusis.ai`
2. Configure bot login credentials in Attendee (same as Option A)
3. Create bots with `teams_settings.use_login: true`

**Advantages:** Works even if the tenant blocks external federation. Guest users are treated as internal.

**Disadvantages:** Requires per-tenant onboarding. The bot account must accept the invitation and may need to consent to the tenant's terms.

---

### Option C: Enable Anonymous Join

**What it does:** Allows anyone with a meeting link to join without authentication.

**Who does it:** Target tenant admin.

**Effort:** One-time, 1-minute admin change.

**Steps for the target tenant admin:**

1. Sign in to [Microsoft Teams Admin Center](https://admin.teams.microsoft.com)
2. Navigate to **Meetings → Meeting policies**
3. Select the relevant policy (e.g., **Global (Org-wide default)**)
4. Find **"Anonymous users can join a meeting"** → Set to **On**
5. Find **"Anonymous users and dial-in callers can start a meeting"** → Set to **On** (optional)
6. Click **Save**

**On the Neusis side:** No changes needed. The bot joins without login.

**Advantages:** Simplest setup. No bot login credentials needed. Fastest join time.

**Disadvantages:** Reduces security posture for the target tenant. Most enterprise orgs won't enable this.

---

### Option D: Bot Account in Target Tenant

**What it does:** Create a dedicated Microsoft account directly in the target tenant for the bot.

**Who does it:** Target tenant admin.

**Effort:** Requires a Teams license ($4-$12.50/month). Per-tenant account.

**Steps for the target tenant admin:**

1. Sign in to [Microsoft Entra Admin Center](https://entra.microsoft.com)
2. Navigate to **Users → All users → Create new user**
3. Create a user:
   - Display name: `Neusis Bot`
   - Username: `neusis-bot@taodigitalsolutions.com` (or similar)
   - Auto-generate or set a password
4. Assign a **Microsoft Teams license** to the user (Teams Essentials or higher)
5. Disable MFA for this service account:
   - Navigate to **Users → Per-user MFA**
   - Find the bot account → Set MFA to **Disabled**
6. Share the username and password with the Neusis team

**On the Neusis side:**

```bash
# Store the target-tenant credentials
kubectl exec -n attendee deployment/attendee-web -- python manage.py shell -c "
from bots.models import Bot, Credentials
project = Bot.objects.first().project
creds, created = Credentials.objects.get_or_create(
    project=project,
    credential_type=Credentials.CredentialTypes.TEAMS_BOT_LOGIN,
)
creds.set_credentials({
    'username': 'neusis-bot@taodigitalsolutions.com',
    'password': '<password-from-tenant-admin>'
})
creds.save()
"
```

**Advantages:** Full access as an internal user. No federation or guest setup needed.

**Disadvantages:** Requires a Teams license ($). Each target tenant needs its own bot account. Attendee currently supports only **one set of Teams login credentials per project** — supporting multiple tenants with different credentials would require code changes.

---

### Option E: Per-Meeting Lobby Bypass (Limited)

**What it does:** The meeting organizer allows everyone to bypass the lobby for a specific meeting.

**Who does it:** The meeting organizer (anyone who creates the meeting).

**Effort:** Must be set for each meeting individually.

**Steps:**

1. Open the meeting in Microsoft Teams or Outlook
2. Click **Meeting options** (in the meeting toolbar or invite)
3. Set **"Who can bypass the lobby?"** → **Everyone**
4. Set **"Who can present?"** → **Everyone** (optional)
5. Save

**Important:** This only works if the tenant admin has **not** disabled anonymous join at the tenant level. If the admin policy blocks anonymous users, per-meeting settings cannot override it.

---

## 6. Option Comparison

| Option | Who Acts | Effort | Recurring | Works if Tenant Blocks Anonymous | Works if Tenant Blocks External |
|---|---|---|---|---|---|
| **A. Federation** | Target admin | One-time | No | Yes (bot signs in) | No |
| **B. Guest invite** | Target admin | One-time + accept | No | Yes | Yes |
| **C. Anonymous join** | Target admin | One-time | No | N/A (enables it) | N/A |
| **D. Bot account in tenant** | Target admin | One-time + license | Monthly cost | Yes | Yes |
| **E. Lobby bypass** | Meeting organizer | Per-meeting | Every meeting | No | No |

**Recommendation priority:** B (guest) > A (federation) > D (dedicated account) > C (anonymous) > E (lobby)

---

## 7. How Other Bot Providers Handle This

| Provider | Join Method | Cross-Tenant Strategy |
|---|---|---|
| **Recall.ai** | Browser automation (like Attendee) | Customers must allowlist the bot domain or enable anonymous join. Offers authenticated join with customer-provided credentials. Documentation guides customers through tenant policy changes. |
| **Fireflies.ai** | Browser automation | Bot joins as anonymous guest by default. Onboarding docs instruct customers to enable lobby bypass. For enterprise, offers SSO-based join. |
| **Otter.ai** | Browser + Microsoft Graph OAuth | User authorizes Otter via OAuth. Bot joins **as the authorizing user** (internal to their tenant). No cross-tenant issue since the bot impersonates an internal user. |
| **Gong / Chorus** | Browser + OAuth service account | Customer authorizes via OAuth during onboarding. Bot signs in as a service account within the customer's tenant. Fully internal access. |
| **Microsoft Teams Bot Framework** | Azure Bot Service API (no browser) | Requires Azure Bot registration. Tenant admin installs the bot as a Teams App. Uses API-based join (Graph API `POST /communications/calls`), not browser. No headless Chrome. |

### Industry Patterns

1. **Self-serve (current Attendee approach):** Bot joins as anonymous/external. Works for permissive tenants. Fails for locked-down enterprises. Lowest onboarding friction.

2. **Customer-configured credentials:** Customer provides a service account in their tenant. Attendee supports this via `teams_settings.use_login`. Works for any tenant. Moderate onboarding friction.

3. **OAuth-based (Otter/Gong model):** Customer authorizes via OAuth flow during setup. Bot acts on behalf of the authorizing user. Best UX but requires building an OAuth integration with Microsoft identity platform. Attendee does **not** support this yet.

4. **Azure Bot Framework (Microsoft-native):** Register an Azure Bot, build a Teams App, distribute via Teams App Store or tenant-level deployment. The bot uses Graph API to join calls programmatically — no browser needed. Highest reliability but significant development effort. Attendee does **not** use this approach.

---

## 8. MFA Considerations

The bot login flow uses browser-based username/password authentication. It does **not** handle MFA prompts (authenticator app, SMS codes, etc.).

**Requirements for the bot account:**

- MFA must be **disabled** or **not enforced** on the bot account
- Or an **App Password** must be generated (bypasses MFA for legacy auth)

### Disable MFA for a service account

1. Sign in to [Microsoft Entra Admin Center](https://entra.microsoft.com)
2. Navigate to **Users → Per-user MFA** (or search "Per-user MFA")
3. Find the bot account
4. Set multi-factor auth status to **Disabled**

### Generate an App Password (if MFA must stay enabled)

1. Sign in to [https://mysignins.microsoft.com/security-info](https://mysignins.microsoft.com/security-info) as the bot account
2. Click **Add sign-in method** → Select **App password**
3. Name it (e.g., "Attendee Bot") → Copy the generated password
4. Use this app password instead of the regular password in bot credentials

> **Note:** App passwords may be disabled by the tenant admin. If the option doesn't appear, ask the admin to enable it: Entra Admin Center → Users → Per-user MFA → Service settings → "Allow users to create app passwords".

---

## 9. Teams Settings Reference

When creating a bot via the API, pass `teams_settings` to control authentication:

```json
{
  "meeting_url": "https://teams.microsoft.com/l/meetup-join/...",
  "bot_name": "Neusis Bot",
  "teams_settings": {
    "use_login": true,
    "login_mode": "only_if_required"
  },
  "recording_settings": { "format": "mp3" },
  "transcription_settings": { "meeting_closed_captions": {} },
  "automatic_leave_settings": {
    "only_participant_in_meeting_timeout_seconds": 60
  }
}
```

| Setting | Values | Description |
|---|---|---|
| `use_login` | `true` / `false` | Whether to use stored Teams login credentials |
| `login_mode` | `"always"` / `"only_if_required"` | `always`: sign in before joining every meeting. `only_if_required`: try anonymous first, sign in only if meeting requires it. |

Credentials are stored **per-project**, not per-bot. All bots in the same project share the same Teams login credentials.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Sign in required" + no login attempt | `teams_settings.use_login` not set on bot | Pass `use_login: true` when creating bot |
| "Sign in required" + "already tried to login" | Authenticated but tenant blocks external users | Ask tenant admin for Option A (federation) or B (guest) |
| "Sign in required" + login redirects to MFA | Bot account has MFA enforced | Disable MFA or use app password (Section 8) |
| Bot joins but stuck in lobby | Organizer hasn't admitted bot | Set lobby bypass to "Everyone" in meeting options |
| "could_not_join_meeting" + "waiting_room_timeout" | Bot waited in lobby too long (default 15 min) | Ask organizer to admit bot or configure lobby bypass |
| Login fails with "incorrect password" | Wrong credentials stored | Update credentials via Django shell |
