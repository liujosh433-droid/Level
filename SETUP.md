# Level - Setup guide

Two paths: **local** (offline, JSON on disk, no GCP needed) and **cloud**
(Cloud Run + Firestore + Vertex).

---

## Local (offline, 60 seconds)

Prereqs: `node >= 20`, `python >= 3.12`, and `[uv](https://docs.astral.sh/uv/)`.
Install `uv` with `brew install uv` or
`curl -LsSf https://astral.sh/uv/install.sh | sh`.

1. `cp .env.example .env` and fill in at least:
  - `LEVEL_ENV=local`
  - `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET`
  - `LEVEL_SESSION_SECRET` (any long random string)
2. `make install`
3. `make dev`
4. Open `http://127.0.0.1:3000`, click **Connect Google**.

**Variable port?** `make dev` binds `:8080` (API) and `:3000` (web). If
you don't have `:8080` available — Cursor grabs it by default, and
plenty of other dev tools do too — flip a single env var:

```bash
LEVEL_API_PORT=8081 make dev
```

The Next.js proxy and the OAuth redirect URI both follow
`LEVEL_API_PORT` automatically — no `.env` edits, no code changes.
The only manual step is registering that port in the Google OAuth
Console once. Do it up-front for the small set of ports listed in
`.env.example` (8080, 8081, 8000) and you never have to touch the
Console again when a port collides.

Want a richer calendar to demo against? Import one of the fixture
calendars in `[example-data/](example-data)` into a scratch Google
account and connect that account:

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