# Level - Setup guide

Two paths: **local** (offline, JSON on disk, no GCP needed) and **cloud**
(Cloud Run + Firestore + Vertex).

---

## Local (offline, 60 seconds)

Prereqs: `node >= 20`, `python >= 3.12`. `uv` is bundled at `.tools/uv`.

1. `cp .env.example .env` and fill in at least:
   - `LEVEL_ENV=local`
   - `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET`
   - `LEVEL_SESSION_SECRET` (any long random string)
2. `make install`
3. `make dev`
4. Open `http://127.0.0.1:3000`, click **Connect Google**.

Don't want to connect a real calendar? `make demo-seed` loads an Alpha/Beta
family with 4 weeks of fake events, usuals, priorities, and a reminder.
Then visit `/today` to see the seeded UI.

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
   Authorized redirect URIs:
   - `http://localhost:8080/v1/auth/google/callback` (local)
   - `https://<api-domain>/v1/auth/google/callback` (cloud)
4. Add **test users**: `testing@devpost.com`, `cloudhackathons@google.com`,
   plus your own email.

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

Source: [`docs/architecture.mmd`](docs/architecture.mmd) -> renders to
`docs/architecture.png`.
