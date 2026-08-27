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

## Regenerating the architecture diagram

```bash
make diagram   # requires @mermaid-js/mermaid-cli
```

Source: `[docs/architecture.mmd](docs/architecture.mmd)` -> renders to
`docs/architecture.png`.