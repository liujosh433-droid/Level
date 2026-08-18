# Level

Caregiver partner for busy parents and multi-generational households.
Reads your Google Calendar, learns your usual weekly rhythm, tracks your
priorities, drafts school emails, and speaks a short summary of your day.

Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/)
in the **Collaborative Partner** track.

## Stack (hackathon mandatory checklist)

- **Gemini 3.5 (Pro + Flash) via Vertex AI** - all agents call
  `packages/core/src/level_core/agents/base.py::call_agent`.
- **Google Agent Development Kit (ADK)** - each agent is an ADK tool; see
  `packages/core/src/level_core/agents/*.py`.
- **Google Cloud** - Cloud Run (API), Cloud Run Jobs (nightly), Firestore
  (state), Vertex AI (model host), Gmail API, Calendar API.

## Architecture

![architecture](docs/architecture.png)

Mermaid source: [`docs/architecture.mmd`](docs/architecture.mmd).

## 60-second local start

Prereqs: `node >= 20`, `python >= 3.12`. A bundled `uv` binary lives at
`.tools/uv` so no global install is needed.

```bash
cp .env.example .env
# Fill in GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET
# (see SETUP.md for OAuth consent-screen instructions).

make install
make dev
# API on http://127.0.0.1:8080, web on http://127.0.0.1:3000
```

Open `http://127.0.0.1:3000`, click **Connect Google**, and Level will
start syncing your calendar. `LEVEL_ENV=local` writes state to
`.level/local_store/` - no GCP needed for demo.

Want to poke around without connecting a real calendar?

```bash
make demo-seed   # Alpha/Beta family with 4 weeks of fake events + usuals
```

## Cloud deploy

See [SETUP.md](SETUP.md). One-liner once terraform is applied:

```bash
make deploy-api && make deploy-jobs
```

## Test

```bash
make test          # unit + security + e2e (all offline)
make test-e2e-web  # Playwright smoke against local dev
```

Coverage target: 85% on `packages/core`, 75% on `packages/api`.

## Hackathon submission

See [SUBMISSION.md](SUBMISSION.md) for the rubric mapping, agent list,
demo video, and judge access instructions.

## Repo layout

```
apps/web           Next.js 15 dashboard (Today / Profile / Contacts / Sources / About)
packages/api       FastAPI on Cloud Run
packages/core      Domain: agents, calendar, care, schedule, email, storage, voice
packages/jobs      Cloud Run Jobs: nightly usuals + TTL + watch renewal
infra/terraform    GCP resources
infra/firestore.rules   Per-user isolation
docs/architecture.mmd   Architecture diagram source
tests/unit         Fast offline tests
tests/security     Prompt-injection corpus + auth + Firestore rules
tests/e2e          Full API flow with LEVEL_ENV=local
```

## License

MIT - see [LICENSE](LICENSE).
