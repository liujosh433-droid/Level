# Devpost submission — draft

Working copy of the text we'll paste into the Devpost submission form. Keep this in sync with what we actually built. Final version submitted by 2026-08-31 5:00 PM PT.

---

## Project title

**Level — see what saying yes crowds out from the care roles you hold.**

## Short tagline (140 chars)

Multi-agent care-role partner (Gemini 3.5 + ADK): mutates a Care Profile from messy calendar/chat data and challenges care collisions before you commit.

## Category

**The Collaborative Partner**

## Inspiration

Modern AI helps busy people say yes faster. Caregivers don’t need more hours invented — they need to see **which care role a yes would crowd out** before they commit. Research on the triple bind, sandwich caregiving, and mental load says the unit of analysis is competing roles under scarce degrees of freedom — not inbox zero.

Level is the friend who won’t let a late networking dinner quietly erase Thursday pickup you marked Keep.

## What it does

Level ingests messy personal signals — Google Calendar, voice-memo transcripts, notes — into a Memory Bank and **mutates a Care Profile** (child care, elder care, paid work, self & recovery, household logistics, co-parent). Users correct with Keep / Not me.

When the user brings a decision — or the background job spots a collision — specialized agents run:

- **Framer** restates the decision precisely (Gemini 3.5 Pro)
- **Retriever** pins care-role facts + vector evidence; loads Care Profile via Agent Gateway (Gemini 3.5 Flash)
- **Challenger** prefers **`role_theft`** challenges (user-facing: care collisions) grounded in sticky windows (Gemini 3.5 Pro + outbound citation guardrails; Conductor retries once on bad output)
- **Judge** scores cognitive biases in the framing (Gemini 3.5 Flash)

**Continuous Action:** `async_challenge` finds events that crowd out confirmed care windows and opens an unsolicited care-collision Decision — no human until they open Today.

## How we built it

**Stack:** Python 3.12, Google ADK, Gemini 3.5 (Pro / Flash / Live) via Vertex AI, FastAPI, Next.js 15, Firestore, Vertex AI Vector Search, Cloud Run + Cloud Run Jobs, Cloud Scheduler, Model Armor, Cloud Storage, Secret Manager, OpenTelemetry → Cloud Trace + Cloud Logging, Terraform.

**Architecture** (see repo README for full diagram): three async loops.

1. **Ingestion loop** — Cloud Scheduler triggers Cloud Run Jobs every 15 min to pull deltas from Google APIs. Signals flow through Model Armor (inbound), the `IngestNormalizer` agent, and land in Firestore + Vector Search.
2. **Session loop** — User opens a session; the `Conductor` (an ADK `SequentialAgent`) runs Framer → Retriever → Challenger → Judge. Each turn is written to Firestore and streamed to the web UI via Firestore `onSnapshot`.
3. **Learning loop** — Nightly + post-session, a Cloud Run Job aggregates bias events into the persistent Bias Profile and regenerates the Manifesto.

**Enterprise-grade components** (built even though our category is Collaborative Partner, for architectural depth): Agent Registry, Agent Runtime, Memory Bank, Agent Identity (per-agent SAs), Agent Gateway (in-process policy router), Model Armor (inbound + outbound), Agent Observability (OTel-instrumented with reasoning-chain traces).

## Challenges we ran into

*(fill in as we go)*

## Accomplishments we're proud of

*(fill in as we go)*

## What we learned

*(fill in as we go)*

## What's next for Level

- Broader ingestion connectors (WhatsApp export, Notion, iMessage)
- Group decision mode (co-parents, family caregivers)
- Long-horizon reflection: "this decision from 3 months ago — what actually happened, and what does that teach us?"

## Built with

`google-adk` `gemini-3.5-pro` `gemini-3.5-flash` `gemini-3.5-flash-live` `vertex-ai` `vertex-vector-search` `model-armor` `firestore` `cloud-run` `cloud-run-jobs` `cloud-scheduler` `cloud-storage` `secret-manager` `cloud-trace` `cloud-logging` `opentelemetry` `terraform` `fastapi` `next.js` `pydantic` `structlog`

## Try it out

- **Live URL:** *(Cloud Run URL — added after deploy)*
- **Public code repo:** *(GitHub URL — added when repo pushed)*
- **Demo video:** *(YouTube URL — added after recording)*
- **Testing instructions:** No login required — landing page includes a "Try the demo caregiver profile" button that runs a full session against a seeded user with pre-ingested demo signals. Judges can also click "Connect Google" for a real OAuth flow if they want to try their own data. From the repo: `make demo-judge` prints Continuous Action proof (ingest → Care Profile version bump → async care-collision Decision → retention prune); open Today for the Care collision banner and Profile for `Care Profile vN`.
- **Access for judges:** Public URL, no restrictions. GitHub repo is public. `testing@devpost.com` and `cloudhackathons@google.com` are added as collaborators as a courtesy.

## Video pitch (4-minute outline)

*(finalize week 3)*

- **0:00–0:20** — Problem: "modern AI is a yes-man." Show a canonical AI conversation where the assistant agrees with everything.
- **0:20–0:40** — Level's twist: warm-but-honest decision partner that cites your own past. Show the Level UI.
- **0:40–2:30** — Live demo: real Cloud Run URL, real Gemini 3.5, real session on a "should I switch my kid's school" decision. Show the Challenger asking a hard question citing an ingested email from three weeks prior. Show the Judge's bias observations updating the Bias Profile in real time via Firestore `onSnapshot`.
- **2:30–3:30** — Architecture proof: Cloud Trace reasoning chain of the session we just ran; Cloud Run dashboard showing the api + jobs services; Firestore console showing the persisted decision and bias events; Model Armor blocking a synthetic prompt-injection attempt during ingestion.
- **3:30–4:00** — Roadmap + close.

## Disclosures

- No pre-existing code was incorporated into this Project. Every line was written during the Submission Period (2026-08-03 to 2026-08-31).
- Standard open-source frameworks and libraries were used (Google ADK, Vertex AI Python SDK, FastAPI, Next.js, Pydantic, structlog, OpenTelemetry, Terraform).
- AI coding assistants (Cursor / Gemini) were used during development.
- All third-party API integrations (Google Calendar, Gmail) are used in accordance with Google API Terms and use standard OAuth consent flows.
