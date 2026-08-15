# Level — GCP setup (do this early)

We want real Vertex / Firestore / Model Armor running as soon as possible.
Don't wait until demo week.

## 1. Tools on your Mac (≈5 min)

```bash
brew install --cask google-cloud-sdk
brew install terraform
brew install --cask docker   # only needed when we start deploying images
```

Then restart your shell (or `source ~/.zshrc`) and confirm:

```bash
gcloud --version
terraform -version
```

## 2. Gemini key for local smoke (today)

1. Rotate the key that was pasted into chat: https://aistudio.google.com/apikey
2. Create a new key.
3. Put it in `.env` (never paste into chat):

```bash
cd /Users/annamokkapati/hack/level
cp .env.example .env
# edit .env — set GOOGLE_API_KEY=...
```

4. Smoke test:

```bash
uv run python scripts/smoke_gemini.py
```

## 3. GCP project + billing (as soon as credit lands)

Credits take up to 72 business hours after the form. When they arrive:

```bash
gcloud auth login
gcloud auth application-default login

# Create the project (or reuse one)
gcloud projects create project-c31bdcdc-f293-47c2-a4c --name="Level Hackathon"
gcloud config set project project-c31bdcdc-f293-47c2-a4c

# Link billing (pick the account that has the $150 credit)
gcloud billing accounts list
gcloud billing projects link project-c31bdcdc-f293-47c2-a4c --billing-account=XXXXXX-XXXXXX-XXXXXX
```

## 4. Terraform apply (real infra — Vertex + Firestore + Model Armor)

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars — confirm project_id

terraform init
terraform plan
terraform apply   # Vector Search deploy can take 20–40 min the first time
```

Then copy outputs into `.env`:

```bash
# from terraform output
LEVEL_ENV=cloud
LEVEL_VECTOR_INDEX_ID=...
LEVEL_VECTOR_INDEX_ENDPOINT_ID=...
LEVEL_VECTOR_DEPLOYED_INDEX_ID=level_signals_deployed
LEVEL_MODEL_ARMOR_TEMPLATE_INBOUND=projects/.../templates/level-inbound
LEVEL_MODEL_ARMOR_TEMPLATE_OUTBOUND=projects/.../templates/level-outbound
```

Re-run the smoke test in cloud mode:

```bash
LEVEL_ENV=cloud uv run python scripts/smoke_gemini.py
```

## Cost expectations while testing early

| Service | While active | Notes |
|---|---|---|
| Vertex Vector Search endpoint | ~$30–90/mo | Biggest line item — keep up during active testing |
| Gemini (Pro/Flash) | cents–dollars/day | Cheap at hackathon volume |
| Firestore / Cloud Run / Storage | free-tier-ish | Negligible |
| Model Armor | cents/call | Negligible |

If you pause for >1 day and want to save money:

```bash
cd infra/terraform
# temporarily: set enable_vector_search = false, then
terraform apply
```

Or tear everything down: `terraform destroy`.

## 5. Real parent test (ChatGPT + Calendar)

### A. ChatGPT-only path (works today, no OAuth)

1. Export ChatGPT: Settings → Data controls → Export data → download zip.
2. `make api` + `make web`
3. Open http://localhost:3000/sources → **Start as guest** → upload the zip.
4. When Memory Bank shows facts → **Ask Level** with a real decision.

Child names are optional on calendar titles: Level still opens a **child care** role from pickup/school/sports cues (shown as “your kids” until a name appears). Named nodes fill in from `Title — Name`, `Jordan's soccer`, ChatGPT/Memory Bank relationship facts, or a Tell Level note like “my kid is Maya.”

Note: with `LEVEL_ENV=local`, memory is in-process — restarting the API clears guest data. For persistence, set `LEVEL_ENV=cloud` + `LEVEL_VECTOR_BACKEND=firestore` (ADC via `gcloud auth application-default login`).

### B. Google Calendar + school-note email

1. In GCP Console → **APIs & Services** → enable **Google Calendar API** and **Gmail API**.
2. **OAuth consent screen** → **Add or remove scopes** → include:
   - `https://www.googleapis.com/auth/calendar.events`
   - `https://www.googleapis.com/auth/gmail.send` (Send email — school / clinic notes)
3. **Credentials** → Create **OAuth client ID** → Application type **Web application**.
4. Authorized redirect URI (exact):

   `http://localhost:8080/v1/auth/google/callback`

5. Put client id/secret in `.env`:

```bash
GOOGLE_OAUTH_CLIENT_ID=....apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8080/v1/auth/google/callback
LEVEL_WEB_APP_URL=http://localhost:3000
```

6. Restart API → Sources → **Connect Google** → sync Calendar. On Google’s screen, leave **Send email** checked.
7. OAuth consent screen: add your Google account as a test user if the app is in Testing. `gmail.send` is a sensitive scope — if it isn’t listed on the consent screen, Google will connect Calendar but Level still can’t send the teacher a note.

### C. What “helpful in reality” looks like

Bring a decision you actually care about (school, job hours, custody logistics). Judge success by whether Level cites *your* ChatGPT/calendar evidence and asks a clarifying question you wouldn’t get from vanilla ChatGPT.
