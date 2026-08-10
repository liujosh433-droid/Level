# Level

**A warm-but-honest AI decision partner for busy caregivers.**

Level is a multi-agent system (Google ADK + Gemini 3.5) that continuously ingests messy personal signals — Google Calendar, voice memos, prior AI chat exports — into a persistent Memory Bank, then acts as the friend who isn't afraid to ask you the hard clarifying question. Instead of the reflexive agreement most AI defaults to, Level asks *what if you're wrong*, cites your own past evidence back at you, and tracks your cognitive biases session over session so you make decisions you can still defend a week later.

> Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/) — **Collaborative Partner** track.

---

## Table of contents

1. [What Level does](#what-level-does)
2. [Architecture](#architecture)
3. [Spin-up: local development](#spin-up-local-development)
4. [Spin-up: cloud deployment](#spin-up-cloud-deployment)
5. [Testing](#testing)
6. [Repo layout](#repo-layout)
7. [Hackathon compliance](#hackathon-compliance)

---

## What Level does

Most AI assistants are sycophants — they agree, encourage, and paper over uncertainty. Caregivers who are already stretched thin need the opposite: a partner who can hold the full context of their life, notice when they're rushing a decision, and ask the question a good friend would ask.

Level ingests three kinds of messy signals and turns them into structured memory:

| Signal type | Source | What we extract |
|---|---|---|
| Calendar events | Google Calendar API | Time pressure, meeting cadence, life rhythm |
| Voice memos | User upload → Gemini transcription | Emotional tone, unspoken preferences |
| Prior AI chats | ChatGPT / Claude / Gemini export drops | What the user has already reasoned through |

When the user brings a decision — *"should I switch my son to the new school?"*, *"do I take the promotion?"* — Level runs a chain of specialized ADK agents:

```
Framer  →  Retriever  →  Challenger  →  Judge
  ↓          ↓             ↓             ↓
restates   pulls        asks the      scores which
the        cited        hard          cognitive biases
decision   evidence     question      showed up in the
                                      user's framing
```

Each turn updates a **Bias Profile** — a persistent, evolving picture of the user's decision-making patterns. Over weeks, Level learns *how* you tend to be wrong so it can push back more precisely.

---

## Architecture

```
                    ┌────────────────────────────────────────────────┐
                    │                    Level                       │
                    └────────────────────────────────────────────────┘
                                            │
        ┌───────────────────────────────────┼───────────────────────────────────┐
        │                                   │                                   │
   ┌────▼─────┐                    ┌────────▼────────┐                    ┌─────▼─────┐
   │   Web    │                    │      API        │                    │   Jobs    │
   │ Next.js  │◀────HTTPS + WS────▶│    FastAPI      │                    │ Cloud Run │
   │ Cloud Run│                    │    Cloud Run    │                    │   Jobs    │
   └──────────┘                    └────────┬────────┘                    └─────┬─────┘
                                            │                                   │
                                            │           ┌───────────────────────┘
                                            │           │  scheduled ingestion
                                            ▼           ▼
                              ┌────────────────────────────────────┐
                              │           Conductor                │
                              │       (Sequential ADK agent)       │
                              │                                    │
                              │  Framer ▶ Retriever ▶ Challenger   │
                              │             ▶ Judge                │
                              └────────┬───────────────────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              ▼                        ▼                        ▼
      ┌───────────────┐        ┌───────────────┐        ┌───────────────┐
      │  Agent        │        │  Agent        │        │  Model Armor  │
      │  Registry     │        │  Gateway      │        │  (inbound +   │
      │  (Firestore)  │        │  (policy      │        │   outbound)   │
      └───────────────┘        │   router)     │        └───────────────┘
                               └───────┬───────┘
                                       │
                        ┌──────────────┼──────────────┐
                        ▼              ▼              ▼
                ┌───────────────┐ ┌──────────┐ ┌────────────────┐
                │  Firestore    │ │  Vertex  │ │  Vertex AI     │
                │  (Memory Bank │ │  Vector  │ │  Gemini 3.5    │
                │   structured) │ │  Search  │ │  Pro/Flash/Live│
                └───────────────┘ └──────────┘ └────────────────┘

               ── every call instrumented via OpenTelemetry → Cloud Trace ──
```

Three asynchronous loops power the system:

- **Ingestion loop** — Cloud Scheduler kicks Cloud Run Jobs every 15 min to pull deltas from Google APIs. Each raw signal passes through Model Armor (PII scrub, prompt-injection block), gets normalized into structured facts by the `IngestNormalizer` agent, embedded into Vertex Vector Search, and indexed in Firestore.
- **Session loop** — The user opens a session (text or Gemini Live voice). The FastAPI service orchestrates the Framer → Retriever → Challenger → Judge chain in a single ADK `SequentialAgent`. Each turn is written to Firestore in real time so the UI can subscribe via `onSnapshot`.
- **Learning loop** — After each session, a background Cloud Run Job aggregates the Judge's bias observations into the user's persistent Bias Profile and updates their "Manifesto" — a self-rewriting statement of what the user says they value, which Level uses to challenge future decisions against past commitments.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the deep dive (data model, failure modes, backpressure, agent contracts).

---

## Spin-up: local development

**Prerequisites:**
- Python 3.12
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv` on macOS)
- A [Google AI Studio API key](https://aistudio.google.com/apikey) — free tier is enough for local iteration

```bash
git clone <this-repo> level && cd level

make env         # copies .env.example → .env
$EDITOR .env     # set GOOGLE_API_KEY to your AI Studio key

make install     # uv sync — installs core, api, jobs and dev deps
make test        # runs the pytest suite (uses fakes; no cloud needed)
make api         # starts the FastAPI service at http://localhost:8080

# in another terminal:
make web-install # once
make web         # Next.js at http://localhost:3000

# optional — seed the demo caregiver narrative (calls Gemini):
make seed
```

By default `LEVEL_ENV=local` uses in-memory fakes for Firestore and Vector Search so you can build and test without any GCP setup. Set `LEVEL_ENV=cloud` and provide `gcloud auth application-default login` credentials to hit real services.

---

## Spin-up: cloud deployment

**One-time setup** (assumes you have the $150 hackathon credit applied to a billing account):

```bash
export PROJECT=project-c31bdcdc-f293-47c2-a4c
export REGION=us-central1

gcloud projects create $PROJECT
gcloud config set project $PROJECT
gcloud services enable \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  cloudtrace.googleapis.com

make gcloud-auth
make tf-init && make tf-apply       # provisions Firestore, IAM, Vector Search, Cloud Run
make deploy-api                     # builds + deploys API to Cloud Run
make deploy-jobs                    # builds + deploys ingestion jobs
```

After `tf-apply` completes, the Vertex Vector Search index endpoint ID is written to Terraform outputs. Put it in `.env` as `LEVEL_VECTOR_INDEX_ENDPOINT_ID`.

The deployed API URL will be printed by `deploy-api`. That URL goes into the Devpost submission.

---

## Testing

```bash
make test           # fast — uses fakes, no network
make test-cov       # + coverage
make check          # lint + type-check + test (what CI runs)
```

Test taxonomy:
- **Unit** (`tests/unit/`) — schemas, guardrails, single agents against fake models
- **Integration** (`tests/integration/`) — full conductor flow against fake memory + fake model, asserts the shape of the produced turns and bias observations
- **Cloud** (marked `@pytest.mark.cloud`, skipped by default) — smoke tests against real Vertex AI + Firestore, run in CI on staging

---

## Repo layout

```
level/
├── README.md                       # this file
├── ARCHITECTURE.md                 # deep-dive: loops, state, failure modes
├── COMPLIANCE.md                   # rules-to-component mapping (living doc)
├── SUBMISSION.md                   # Devpost submission text draft
├── pyproject.toml                  # uv workspace root
├── Makefile                        # every dev/deploy command
├── packages/
│   ├── core/                       # shared library: agents, memory, guardrails, models
│   ├── api/                        # FastAPI, Cloud Run service
│   └── jobs/                       # Cloud Run Jobs (ingestion + async challenge)
├── apps/
│   └── web/                        # Next.js UI (session + landing)
├── scripts/                        # smoke_gemini, seed_demo_data, seed_bias_taxonomy
├── tests/                          # unit + integration
├── infra/
│   ├── terraform/                  # every GCP resource, reproducible
│   └── cloudbuild-*.yaml           # Cloud Build pipelines
└── docs/
    └── diagrams/architecture.svg   # rendered version of the ASCII diagram above
```

---

## Hackathon compliance

Full component-by-component mapping is in [`COMPLIANCE.md`](./COMPLIANCE.md). Highlights:

- **Mandatory Gemini 3.5+** — Vertex AI Gemini 3.5 Pro (reasoning), Flash (routing), Live (voice)
- **Mandatory Google Agent Framework** — Google ADK for every agent
- **Mandatory Google Cloud service** — Cloud Run, Firestore, Cloud Storage, Cloud Scheduler, Vertex AI, Secret Manager (6 services)
- **Category** — Collaborative Partner (with Fortified Enterprise Fleet architectural components layered in for bonus points)
- **Rubric alignment** — Multi-agent Nexus + Evolving Knowledge Engine + Unlikely Hero (single caregivers)
- **Bonus components** — Agent Registry, Agent Runtime, Memory Bank, Agent Identity, Agent Gateway, Model Armor, Agent Observability — all present
