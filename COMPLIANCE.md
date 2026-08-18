# Compliance — Level ↔ hackathon rules

Living document mapping every rule and bonus lever in the [All Things Agentic Hackathon rules](https://allthingsagentichackathon.devpost.com/rules) to a concrete component in this repo. Updated as we build.

## Category selection

**The Collaborative Partner** — Level asks clarifying questions about **care collisions**, guides caregivers step-by-step, and captures structured feedback (Keep / Not me on care roles, `Judge` bias events, self-rewriting Manifesto + mutating **Care Profile**) so it constantly adapts to how *this* caregiver holds competing roles.

We additionally implement several **Fortified Enterprise Fleet** architectural components (Agent Registry, Memory Bank, Model Armor, Agent Observability, Agent Identity, Agent Gateway on the Retriever hot path) to strengthen the *Architectural Discipline* score.

---

## Mandatory requirements

| Requirement | How we satisfy it | Location |
|---|---|---|
| Gemini 3.5 or newer via Gemini API or Vertex AI | Vertex AI Gemini 3.5 Pro (challenger, framer, judge reasoning), 3.5 Flash (routing, ingestion normalization), 3.5 Flash Live (voice sessions). Local dev fallback uses AI Studio API. Model IDs are env-configurable and default to `gemini-3.5-*`. | `packages/core/src/level_core/models/gemini.py`, `.env.example` |
| At least one Google Agent Framework | **Google ADK** — every agent is an `LlmAgent`. `Conductor` is a `SequentialAgent`. Function tools are plain Python functions with type hints, registered via the `tools=` parameter. | `packages/core/src/level_core/agents/` |
| At least one Google Cloud infrastructure service | We use **six**: Cloud Run (api + jobs + web), Firestore (state), Cloud Storage (raw signals), Cloud Scheduler (ingest cron), Vertex AI (Gemini + Vector Search + Model Armor), Secret Manager (API keys). | `infra/terraform/` |
| Project newly created during Submission Period | Repo initialized 2026-08-08; commit history public in git | Git history |
| Third-party integrations authorized | Only Google APIs (Calendar, Gmail) — used per Google Terms; users grant OAuth consent | `packages/core/src/level_core/ingest/` |
| Testing access provided | Hosted Cloud Run URL published in Devpost submission; API key-free public demo mode | `SUBMISSION.md`, `packages/api` |
| English language | All UI text, prompts, docs in English | Everywhere |

---

## Judging rubric alignment

### Innovation & Operational Utility (40%)

| Sub-criterion | Level's answer |
|---|---|
| Eliminates real-world friction | Caregivers under scarce degrees of freedom — Level holds competing care roles and makes **care collisions** visible before they say yes. |
| "Twist" is present | Fixed care-role taxonomy + care-collision Challenger (`role_theft` type) + anti-sycophancy + Keep/Not me. |
| Continuous Action Engine — background multi-step workflow | Yes — `async_challenge` job detects calendar↔care-window collisions and opens an unsolicited Conductor turn (`origin=async_role_theft`) with no human in the loop. Ingest job mutates Care Profile after signals land. |
| Bring Your Own Friction — personal problem | Yes — busy parents / sandwich / working caregivers (Unlikely Hero). |
| Evolving Knowledge Engine — synthesizes/mutates data | Yes — Care Profile is **mutated** on ingest/sync (`persist_care_profile_from_events`); Manifesto regenerates from care roles; Keep/Not me mutates salience/status. |
| Ingests messy unstructured data | Yes — Google Calendar, voice-memo transcripts (fixtures), free-text notes. (Gmail connector not shipped yet.) |
| Multi-Agent Nexus — task warrants multi-agent | Yes — Framer → Retriever (care-pinned) → Challenger (`role_theft`) → Judge; Conductor retries once on invalid/hallucinated Challenger output. |
| Delegates to specialized sub-agents | Yes — see above; Retriever loads Care Profile via Agent Gateway tool `get_care_profile`. |
| Built for Unlikely Hero | Single parents / caregivers, not corporate roles. |

### Architectural Discipline & Tech Stack (30%)

| Sub-criterion | Level's answer |
|---|---|
| Modularized, maintainable | `uv` workspace with `core`, `api`, `jobs` — clean separation. Each agent, guardrail, and repository is its own module with a Pydantic-typed contract. |
| State management | Firestore for structured state, Vertex Vector Search for semantic memory, in-process session state via ADK's `output_key` mechanism. No global mutables. |
| Tools isolated and scoped for security | Each agent has its own Google Cloud service account with narrowly scoped IAM. Agent Gateway enforces per-agent tool allowlists at runtime. |
| Intelligent schema design | Every inter-agent payload is a Pydantic model with strict validation. Firestore docs versioned; ingestion is idempotent. |
| Efficient vector embedding | Single multi-tenant Vector Search index with `user_id` restrict; 500-token chunks with 50-token overlap; `text-embedding-004` (768-d). |
| Manages massive context windows | Retriever role-pins ≤~10 facts; care snippet ≤800 chars. **Implemented** retention: `level-retain` job prunes EVENT facts past 90d TTL + soft-caps at 150 facts/user without deleting Keep’d/cited pins ([`retention.py`](./packages/core/src/level_core/memory/retention.py)). GCS cold archive = scale-up only — see [`ARCHITECTURE.md`](./ARCHITECTURE.md#memory-retention-context-limits-and-scale-judges). |
| Strict separation of concerns between agents | Enforced by per-agent SAs (IAM) + Pydantic contracts + Agent Gateway. |
| Failure-tolerant inter-agent routing | Conductor **retries Challenger once** with a repair prompt on `InvalidAgentOutput` or hallucinated citations, then degrades/blocks with an audit event. Outbound `fact_id` guardrail is enforced. |

### Demo & Production Readiness (30%)

| Sub-criterion | Level's answer |
|---|---|
| Unedited live execution in video | 4-min demo will show: real Cloud Run URL, live session, Cloud Trace reasoning chain, Firestore doc updates, Cloud Run logs. Recorded in one take. |
| Clean architecture diagram | ASCII in README + rendered SVG in `docs/diagrams/architecture.svg`. |
| Reproducible setup instructions | Terraform for infra + Makefile for every command + one-command deploy (`make deploy-api`). |
| Visual proof of Cloud deployment | Cloud Run dashboard shown in demo video; `*.run.app` URL live during judging. |

---

## Gemini Enterprise Agent Platform (bonus components)

Even though our primary category is Collaborative Partner, we implement every recommended enterprise architecture component:

| Component | Implementation | Location |
|---|---|---|
| **Agent Registry** | Firestore-backed registry storing each agent's version, prompt SHA, model, owner, and IAM binding. `AgentRegistry.discover()` returns all registered agents. Every registered agent is queryable by name + version. | `packages/core/src/level_core/agents/registry.py` |
| **Agent Runtime** | Cloud Run Jobs for long-running async work (ingestion + async challenge generation). Session-scoped agent runs use ADK's `Runner` with `InMemorySessionService` (local) or Firestore-backed session service (cloud). | `packages/jobs/` |
| **Memory Bank** | Firestore (structured facts, decisions, turns, bias events) + Vertex AI Vector Search (semantic recall). Cross-session context via persistent user sub-collections. | `packages/core/src/level_core/memory/` |
| **Agent Identity** | Per-agent Google Cloud service accounts with narrowly scoped IAM; Conductor impersonates via `iam.serviceAccountTokenCreator`. Zero-trust between agents. | `infra/terraform/iam.tf`, `packages/core/src/level_core/identity/` |
| **Agent Gateway** | In-process policy router: agents call `gateway.invoke(tool_name, args)`; gateway enforces per-agent tool allowlists and rate limits. Every invocation is an OTel span. | `packages/core/src/level_core/gateway/` |
| **Model Armor** | Two-point deployment: inbound (PII scrub + prompt-injection block on every ingested signal) and outbound (tone contract + tool-poisoning block on every challenger response). | `packages/core/src/level_core/guardrails/`, `infra/model_armor/policies.yaml` |
| **Agent Observability** | OpenTelemetry-instrumented via `@traced` decorator on every agent call. Exported to Cloud Trace + Cloud Logging. Every reasoning step visible as a span with `agent.name`, `agent.version`, `model.id`, `prompt.sha`, `tokens.*`, `latency_ms`. | `packages/core/src/level_core/observability/` |

---

## Bonus contributions (Stage Three)

| Bonus | Status | Max points |
|---|---|---|
| Public blog / video content about how the project was built (with hashtag disclosure) | Planned week 3 — dev.to post | +0.2 |
| Social media post with `#AllThingsAgenticHackathon` | Planned week 3 — LinkedIn + X | +0.2 |
| Additional Google AI model — **Gemma** (on-device PII classifier for extra defense in Model Armor pipeline) | Planned | +0.2 |
| Additional Google AI model — **Veo** (weekly "your week in decisions" recap video, auto-generated) | Planned week 3 if time | +0.2 |
| Additional Google AI model — **Lyria** (ambient audio in reflection UI) | Stretch | +0.2 |

**Estimated bonus points ceiling**: +0.6 for extra models (cap) + 0.2 blog + 0.2 social = **1.0** on top of the base 1–5 score.

---

## Prize eligibility

Given our category and architecture, Level is eligible for:
- **Grand Prize** ($40k)
- **The Collaborative Partner** category prize ($20k)
- **Best Architectural Design** ($5k) — architectural depth via Fortified Enterprise Fleet components
- **Best Multimodal UX** ($5k) — voice via Gemini Live + text + planned Veo output
- **Individual/Hobbyist** ($10k) — if built solo
- **Honorable Mentions** ($2k × 5)

---

## Open compliance items (track here)

- [ ] Devpost account created + team registered
- [ ] Public GitHub repo created + repo URL added to submission
- [ ] Testing access instructions in `SUBMISSION.md` include a demo login (or public demo mode)
- [ ] Architecture diagram rendered to SVG (currently only ASCII)
- [ ] 4-min demo video recorded, uploaded to YouTube (public), URL added to submission
- [ ] Cloud Run URL is publicly accessible (or private with `testing@devpost.com` + `cloudhackathons@google.com` invited)
- [ ] Bonus blog post published + link in submission
- [ ] Bonus social post published + link in submission
- [ ] At least one additional Google AI model integrated for bonus points
- [ ] `README.md` includes visible language: "no pre-existing code — built entirely during 2026-08-03 to 2026-08-31 Submission Period"
