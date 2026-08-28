# Level - Setup guide

Two paths: **local** (offline, JSON on disk, no GCP needed) and **cloud**
(Cloud Run + Firestore + Vertex).

---

## Local (2 minutes, no Google account required for OAuth)

Prereqs: `node >= 20`, `python >= 3.12`, and
`[uv](https://docs.astral.sh/uv/)`. Install `uv` with `brew install uv`
or `curl -LsSf https://astral.sh/uv/install.sh | sh`.

The recommended local path uses **zero-OAuth demo mode** plus a
**free Gemini API key** — no Google Cloud project, no OAuth client,
no calendar import, but real LLM responses.

**Step 1 — Get a free Gemini API key (~60 seconds).** Level's chat,
email drafting, and "Hear my day" summary are LLM-generated. Without
a key they still return deterministic template responses so the app
never breaks, but you'll be looking at the fallback prose (see
["Skipping the key"](#skipping-the-key-what-you-lose) below) — not
Level actually reasoning about the caregiver's day. To see the
project as designed, grab a key:

- Open [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- Sign in with any Google account, click **Create API key**
- No credit card, no GCP project, no billing enabled

Free tier is ~15 req/min and ~1M tokens/day on `gemini-2.5-flash` — plenty for a full judging session. I deliberately don't ship a shared key to this public repo.

**Step 2 — Bring up the app.**

```bash
cp .env.example .env
# Open .env and paste your key into the GOOGLE_API_KEY= line.
# Everything else can stay at defaults - GOOGLE_OAUTH_* stays blank.
make install
make dev
```

**Step 3 — Try the demo.** Open `http://127.0.0.1:3000` and click
**Try demo: Solo caregiver** on the landing page.

That click hits `POST /v1/auth/demo`, which loads
`example-data/caregiver-month-solo.ics` into a synthetic user, drops
the same signed session cookie a real OAuth callback would, and
returns to `/today` with 200+ pre-classified events, a curated cast
of people, and 6+ missing-usuals for the current week already
computed.

Solo caregiver is the primary demo because the single-parent
workload is the more distinctive story and the one that best
showcases RoleAgent inference. **Or: Two-parent family** is a
second option that adds a co-parent (Alex) so you can see how
Level splits pickup duty across two adults.

Demo mode is `LEVEL_ENV=local` only. The endpoint 404s in cloud so a
probe can't spawn synthetic users against the deployed API.

Behavioral guardrails in demo mode (with or without an API key):

- **Email sending** short-circuits to a "preview" response — Level
logs the send, clears the pending draft, but never actually hits
Gmail. So a judge can click **Send** on a drafted email without
emailing anyone.
- **Calendar edits** (book / move / delete) chat responses degrade to
a friendly "connect Google to actually book" message; the seeded
agenda is read-only.

### Demo real-send mode (optional, for demo recordings)

If you want a demo recording to include *actual email proof* (open
your inbox on screen, show the drafted note landed there), demo
mode has an escape hatch that flips the email-preview
short-circuit into a real Gmail send — with a hard intercept on
the recipient so mail can never leak to a fake demo contact.

Three env vars, all required to arm it (any missing → preview):

```bash
# Master toggle. False by default so nobody accidentally sends.
LEVEL_DEMO_SEND_REAL_EMAILS=true

# Every demo send is rewritten to this address. Point at YOUR own
# inbox so the drafted "to teacher@example.com" lands in a mailbox
# you can screencap.
LEVEL_DEMO_EMAIL_INTERCEPT_TO=you@example.com

# Refresh token for the Gmail account that will actually send.
# Do the normal OAuth flow against your own Google account once
# and grab the refresh token from `.level/local_store/<uid>/tokens.json`.
LEVEL_DEMO_GMAIL_REFRESH_TOKEN=1//0e...long-token
```

When armed:

- The endpoint still returns `demo: true` (so the UI shows the
same preview banner), plus `demo_real_send: true`, `drafted_to`,
and `delivered_to` for observability.
- The drafted `to` in the response is the *pretend* recipient
(e.g. "Ms. Anna"), never the intercept address — nothing about
the demo UI changes.
- The wire call to Gmail always uses the intercept address as the
recipient, so no matter what the AI drafts, the mail lands in
your own inbox.

Real-send stays gated on `is_demo_user(profile)` — a real
signed-in user's `/email/send` never enters this branch even if
the env vars are set.

**Enabling it in Cloud Run (optional).** The two non-secret vars
ride along as plain env; the refresh token goes into Secret
Manager. If you use the bundled Terraform module
(`infra/terraform/`), add to your `terraform.tfvars`:

```hcl
demo_send_real_emails    = true
demo_email_intercept_to  = "you@example.com"
demo_gmail_refresh_token = "1//0e...long-token"   # marked sensitive
```

then `terraform apply`. The module provisions a
`level-demo-gmail-refresh-token` secret, mounts it into the API
service, and skips creating it entirely when
`demo_send_real_emails = false` (the default).

To wire it without Terraform, run once:

```bash
# 1. Create the Secret Manager entry.
printf %s "$LEVEL_DEMO_GMAIL_REFRESH_TOKEN" \
  | gcloud secrets create level-demo-gmail-refresh-token \
      --replication-policy=automatic --data-file=-

# 2. Grant the Cloud Run SA read access (skip if you use the TF
#    module - it grants project-level secretAccessor already).
gcloud secrets add-iam-policy-binding level-demo-gmail-refresh-token \
  --member="serviceAccount:$(gcloud run services describe level-api \
      --region "$GOOGLE_CLOUD_REGION" \
      --format='value(spec.template.spec.serviceAccountName)')" \
  --role=roles/secretmanager.secretAccessor

# 3. Update the running service.
gcloud run services update level-api --region "$GOOGLE_CLOUD_REGION" \
  --update-env-vars="LEVEL_DEMO_SEND_REAL_EMAILS=true,LEVEL_DEMO_EMAIL_INTERCEPT_TO=you@example.com" \
  --update-secrets="LEVEL_DEMO_GMAIL_REFRESH_TOKEN=level-demo-gmail-refresh-token:latest"
```

To rotate the token, add a new secret version
(`gcloud secrets versions add level-demo-gmail-refresh-token
--data-file=-`) — Cloud Run picks it up on the next cold start
because the mount uses `:latest`.

### Skipping the key: what you lose

You can boot without `GOOGLE_API_KEY` and the app will still run —
the endpoints return 200 and nothing 500s. But every LLM path
degrades to a deterministic template:

- **Chat** → intent-matched canned replies (still routed correctly
by the fast-path regex + gate; you just won't see Gemini generate
novel prose)
- **"Hear my day"** → a real 2-3 sentence summary synthesized from
the seeded events + missing usuals + reminders, but written by
the fallback function (`voice.summary._fallback_summary`) not by
Gemini
- **Email drafting** → a stock template with names/times filled in
from the event, not an LLM-composed message
- **Nightly proactive cards, RoleAgent inference, priority ranking**
→ skipped entirely (they're LLM-only paths)

Use the no-key path only for a smoke test or if you can't reach
`aistudio.google.com` — it's not the intended demo experience.

**Variable port?** `make dev` binds `:8080` (API) and `:3000` (web).
Both are common dev-tool ports — the **Cursor IDE holds both by
default** for its own agent-bridge, and other dev servers routinely
grab them too. If you're on Cursor (or any of them collide), flip
whichever port(s) you need:

```bash
# Cursor is on 8080 only:
LEVEL_API_PORT=8081 make dev

# Cursor is on both 8080 AND 3000 (common):
LEVEL_API_PORT=8081 LEVEL_WEB_PORT=3100 make dev
```

Silent-failure warning for the web port: Next.js's "Ready in Xms"
message can print even when Cursor is squatting on `127.0.0.1:3000`
— Next successfully binds the IPv6 wildcard, but the browser
connects via the IPv4 loopback that Cursor has, gets `ERR_CONNECTION_RESET`,
and you're left staring at a "Ready" server that isn't. The fix is
always to move the web port off 3000.

The Next.js proxy and the OAuth redirect URI both follow
`LEVEL_API_PORT` automatically — no `.env` edits, no code changes.

---

## Hosted demo (in cloud)

The judge-facing deployment on Cloud Run exposes the same demo
button on its landing page — no cloning, no API key on the judge's
side, ~10 seconds from URL to interactive `/today`. It's the same
`POST /v1/auth/demo` handler as the local path; the only
differences are:

- **Enabled with a feature flag.** Off by default in cloud
  (`LEVEL_DEMO_IN_CLOUD=false` — the endpoint 404s), on for the
  hackathon deployment (`LEVEL_DEMO_IN_CLOUD=true`). The flag is a
  security fence: without it, an attacker who guessed the URL could
  spawn synthetic users against the deployed API + Vertex billing.
- **Bounded user pool.** Judge's client IP is SHA-256 hashed to a
  slot in `[0, LEVEL_DEMO_SLOTS_PER_SCENARIO)` (default 3). The
  slot maps to a fixed user id (`u_demo_solo_0`, `u_demo_solo_1`,
  `u_demo_solo_2`, and same for `family`). Total demo population is
  capped at `slots * scenarios = 6 user records` no matter how much
  traffic hits the endpoint. Same judge from the same IP lands on
  the same user across clicks (session state persists).
- **Per-IP rate limit.** Token bucket on `/v1/auth/demo` itself:
  `LEVEL_DEMO_PER_IP_PER_HOUR` (default 10) capacity, refilled at
  the same rate per second. A bot spamming demo logins from one IP
  gets 429'd before it can burn through Firestore or LLM budget.
- **Uses the deployment's Gemini quota.** Since the request lands
  on our Cloud Run service, chat and "Hear my day" call the
  configured Gemini backend (Vertex ADC in cloud) — the judge sees
  real LLM output without providing any API key. Per-user daily
  cost cap (`$2`) still applies to each demo slot, so worst case is
  `$2 × 6 = $12/day` even under adversarial load.

### Enabling cloud demo on your own deployment

**Important:** Cloud Run reads environment variables from the
service configuration, not from any `.env` file baked into the
image. Editing `.env` and redeploying will not flip the flag.
Choose whichever path matches how you deploy:

**One-shot with `gcloud` (fastest):**

```bash
gcloud run services update level-api \
  --region=us-central1 \
  --update-env-vars=LEVEL_DEMO_IN_CLOUD=true
```

Cloud Run restarts the container with the new env — no image
rebuild needed. Add `LEVEL_DEMO_SLOTS_PER_SCENARIO` and
`LEVEL_DEMO_PER_IP_PER_HOUR` in the same call if you want to
override the defaults.

**Persistent via Terraform:**

Set `demo_in_cloud = true` in `infra/terraform/terraform.tfvars`
and run `make tf-apply`. The variable is already wired into
[`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)
along with the two optional pool + rate-limit knobs
(`demo_slots_per_scenario`, `demo_per_ip_per_hour`). This is the
recommended path because it survives future `terraform apply`
runs — a `gcloud`-only change would be reverted the next time
Terraform reconciles the service.

**Persistent via Console:**

Cloud Run → your service → **Edit & Deploy New Revision** →
**Variables & Secrets** → add `LEVEL_DEMO_IN_CLOUD=true`.

**Verify:**

```bash
curl https://<your-cloud-run-url>/v1/config/features
# Look for: "demo": {"available": true, "scenarios": [...]}
```

If you already had the site open, hard-refresh (Cmd/Ctrl-Shift-R)
to bust the cached `/v1/config/features` response — otherwise the
frontend still thinks demo is off.

---

## Local with your real Google Calendar (optional)

For end-to-end testing with your own calendar and Gmail, follow the
OAuth-consent-screen section below to create your own OAuth client,
then set `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` in
`.env` and click **Connect Google** on the landing page instead of
the demo button. Also set `LEVEL_SESSION_SECRET` to a long random
string.

Want a richer calendar to test against without connecting a real one?
Import one of the fixture ICS files into a scratch Google account and
connect that account:

- `example-data/caregiver-month.ics` — two-parent family with a
co-parent. Two usuals are missing in the current demo week so the
proactive-cards job has something to surface immediately.
- `example-data/caregiver-month-solo.ics` — single caregiver, same
kids and same elder-care parent, no co-parent.

To (re)generate either file:

```bash
uv run --package level-jobs python -m level_jobs.make_caregiver_ics --scenario both
```

---

## Google OAuth consent screen

1. GCP Console -> **APIs & Services -> Enabled APIs & Services** -> enable:
  - Google Calendar API
  - Gmail API
  - Vertex AI API (cloud only)
2. **OAuth consent screen** -> External -> add scopes:
  - `openid`, `email`, `profile`
  - `https://www.googleapis.com/auth/calendar`
  - `https://www.googleapis.com/auth/calendar.events`
  - `https://www.googleapis.com/auth/gmail.send`
3. **Credentials** -> Create OAuth client -> **Web application** ->
  Authorized redirect URIs. Register **all three local ports at once**
  so port collisions never send you back to the Console:
  - `http://localhost:8080/v1/auth/google/callback` (default)
  - `http://localhost:8081/v1/auth/google/callback` (Cursor fallback)
  - `http://localhost:8000/v1/auth/google/callback` (common alt)
  - `https://<api-domain>/v1/auth/google/callback` (cloud)
4. Add **test users**: your own email, plus any additional Google
  accounts you want to grant sign-in access. While the app is in
   OAuth testing mode, only listed test users can sign in.

---

## Cloud deploy (Cloud Run + Firestore + Vertex)

1. `gcloud auth login` and `gcloud config set project <YOUR_PROJECT>`.
2. Copy tfvars and fill in:
  ```bash
   cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
   $EDITOR infra/terraform/terraform.tfvars
  ```
3. Provision infra:
  ```bash
   make tf-init && make tf-apply
  ```
   This creates Firestore, Artifact Registry, per-service SAs, IAM,
   Cloud Run service (stub), nightly Cloud Run Job, Cloud Scheduler trigger,
   and Secret Manager entries for `LEVEL_SESSION_SECRET` and the OAuth
   client secret.
4. Publish Firestore rules:
  ```bash
   gcloud firestore databases update --database='(default)' \
     --location=us-central1
   gcloud firestore security-rules deploy infra/firestore.rules
  ```
5. Build + deploy images:
  ```bash
   make deploy-api
   make deploy-jobs
  ```
6. Grab the API URL from `terraform output api_url` and set it as
  `LEVEL_PUBLIC_API_URL` on the API service (for calendar webhook), then
   re-deploy.
7. Update your OAuth consent screen redirect URI to match the new API URL.

---

## Cost guardrails

- `LEVEL_DAILY_COST_CAP_USD` defaults to $2/user/day. When exceeded, the
agents/gate.py drops non-chat AI calls until midnight PT.
- `LEVEL_USER_RATE_PER_HOUR` / `LEVEL_USER_RATE_PER_DAY` cap raw call
counts. Both stored in the audit log so `/admin/traces` shows spend.

## Multimodal recap + chime (Veo 3 + Lyria)

Off by default. The `/week` page renders a "This week's recap"
tile that stays as a friendly placeholder unless you turn media on;
"Hear my day" plays a Lyria chime intro when it's on.

```bash
LEVEL_MEDIA_ENABLED=true
LEVEL_MODEL_VEO=veo-3.1-fast-generate-001      # default (Vertex GA)
LEVEL_MODEL_LYRIA=lyria-3-clip-preview         # default
```

Model-ID gotcha: on Vertex AI (`vertexai=True` in the SDK client)
the Veo 3.1 model IDs end in `-001` — `veo-3.1-generate-001` for
the standard model or `veo-3.1-fast-generate-001` for the fast /
cheaper variant we default to. The Gemini API path uses
`-preview` suffixes; using those with Vertex returns 404. Veo 3.0
preview was retired in April 2026, which is why the older
`veo-3.0-generate-preview` default silently failed.

To actually reach the models you also need:

- `GOOGLE_CLOUD_PROJECT` set and Vertex AI enabled.
- Veo 3.1 (Standard or Fast) available in your project — GA as of
  Nov 2025 in `us-central1`. Check with
  `gcloud ai models list --region=us-central1 | grep veo`.
- Lyria 3 access on the same project. Lyria only serves from
  region `global`; the code hardcodes this, so you don't need to
  change `GOOGLE_CLOUD_REGION`.

Behavior when on but the model is unavailable (no quota, no access,
wrong region): the endpoint returns `{ready: false, reason: "..."}`,
and the UI shows the placeholder with a plain-English explanation.
It never breaks the page.

Cost: Veo 3.1 Fast is ~$0.50-1 per 15-second 720p clip; Veo 3.1
Standard is roughly 2x that. The endpoint caches per user per ISO
week so a judge clicking around only pays for one clip a week.
Lyria clips are cached at the app level per mood (three total).

## Regenerating the architecture diagram

```bash
make diagram   # requires @mermaid-js/mermaid-cli
```

Source: `[docs/architecture.mmd](docs/architecture.mmd)` -> renders to
`docs/architecture.png`.