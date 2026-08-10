# Architecture

This doc is the technical companion to [`README.md`](./README.md). It's structured for a judge or engineer who wants to understand *why* the system is built the way it is — the invariants, the failure modes, and the boundaries.

---

## Design principles

1. **Failure-tolerant over feature-complete.** Every agent call is a network call to a non-deterministic model. Retries, timeouts, output-schema validation, and hallucination guards are first-class.
2. **Strictly separated concerns.** No agent knows about another agent's internal state. Agents communicate through Pydantic-typed structured outputs written to Firestore. This is a strict architectural rule — enforced by the fact that agents have per-agent service accounts with narrowly scoped IAM.
3. **Fakes for everything.** Every external dependency (Firestore, Vector Search, Gemini) has an abstract interface and a fake implementation. Every unit test uses fakes; no network in the fast test path.
4. **Configuration lives in one place.** [`level_core.config.Settings`](./packages/core/src/level_core/config.py) is the single source of truth. Everything else reads from it.
5. **Observability is not optional.** Every agent call is a span. Every span carries the prompt version and model. Traces map 1:1 to reasoning chains.

---

## The three loops

Level's behavior emerges from three loops that run at very different cadences.

### 1. Ingestion loop (every 15 min)

```
Cloud Scheduler ─► Cloud Run Job ─► Google Calendar / Gmail delta pull
                                          │
                                          ▼
                                  Model Armor inbound
                                  (PII scrub, prompt-injection block)
                                          │
                                          ▼
                                  IngestNormalizer agent
                                  (Gemini Flash — structured extraction)
                                          │
                                          ▼
                              ┌───────────┴───────────┐
                              ▼                       ▼
                         Firestore              Vertex Vector Search
                       (structured Fact)         (embedded chunk)
```

- Long-running, resumable, idempotent. Each source has a `last_seen_at` cursor persisted per user in Firestore.
- Rate-limit-aware: Google APIs return `429` and we back off with jitter.
- All facts are versioned — if the same event is edited upstream, we don't overwrite, we append a new version and mark the old one superseded. This lets the Retriever agent notice when a user changed their mind.

### 2. Session loop (real-time, human-triggered)

```
User opens session (web or voice) ─► API creates Decision doc in Firestore
                                          │
                                          ▼
                                  Conductor (SequentialAgent)
                                          │
                          ┌───────────────┼───────────────────────┐
                          ▼               ▼                       ▼
                       Framer         Retriever              Challenger
                      (Pro,           (Flash + Vector       (Pro, streamed
                       output=            Search)            through Model
                       DecisionFrame)                        Armor outbound)
                                                                   │
                                                                   ▼
                                                                Judge
                                                              (Flash,
                                                               output=list[BiasEvent])
                                          │
                                          ▼
                              write Turn to Firestore
                                          │
                                          ▼
                              UI updates via onSnapshot
```

- The Conductor is a `SequentialAgent` — deterministic ordering, sub-agent outputs written to shared session state via `output_key`.
- Streaming: the Challenger's response streams over Server-Sent Events to the web client, while the Judge runs in parallel over the completed transcript.
- If any sub-agent fails or returns invalid output, the Conductor retries once with a stricter output schema. Second failure → the turn is marked degraded and Level admits it to the user ("I couldn't find enough context for a good challenge — can you tell me more?"). We never fabricate.

### 3. Learning loop (nightly + post-session)

```
Cloud Scheduler (nightly) ─┐
                           │
Post-session trigger ──────┼─► Cloud Run Job: aggregate BiasEvents into BiasProfile
                           │        │
                           │        ▼
                           │   Update Manifesto (self-rewriting statement of user's
                           │   professed values, computed from what they've said
                           │   they care about across sessions)
                           │        │
                           │        ▼
                           │   Store versioned Manifesto in Firestore
```

- The Bias Profile is a numeric vector over the taxonomy in [`level_core.bias.taxonomy`](./packages/core/src/level_core/bias/taxonomy.py) — smoothed EMA per bias, plus a streak count and a "how much did this bias affect *outcomes* the user later regretted" retrospective score.
- The Manifesto is generated by Gemini Pro from the union of value-claims across sessions, then written as a versioned document. Challenger reads the current Manifesto before every session so it can catch "you said X three weeks ago" moments.

---

## Data model (Firestore, native mode)

```
users/{user_id}
  ├── profile                    (User doc — settings, connected sources)
  ├── manifesto                  (latest Manifesto doc)
  ├── bias_profile               (aggregated BiasProfile)
  ├── manifesto_versions/{ver}   (history)
  ├── signals/{signal_id}        (raw ingested unit)
  ├── facts/{fact_id}            (structured extraction from a signal)
  ├── decisions/{decision_id}
  │      ├── (Decision doc — subject, status, opened_at)
  │      └── turns/{turn_id}     (Turn — user prompt + agent responses + bias events)
  └── bias_events/{event_id}     (cross-decision index for aggregate queries)

agents/{agent_name}/versions/{version}  (Agent Registry: prompt + model + owner + IAM)
```

Design notes:
- Sub-collections keep queries scoped and cheap.
- `bias_events` is duplicated (denormalized) into a top-level user sub-collection for cross-decision analytics without expensive collectionGroup queries.
- Every doc carries `created_at`, `updated_at`, `written_by` (which agent version), `trace_id` (OpenTelemetry).

---

## Vector store (Vertex AI Vector Search)

- One index per user tenant boundary is expensive; instead we use a **single multi-tenant index** with a `user_id` restrict on every embedding. Filtering by `user_id` at query time is enforced in `level_core.memory.vector_store` — the raw index is never queried without a user restrict.
- Embeddings are `text-embedding-004` (768-d). Cheap and accurate for our text-only signals.
- Chunk size ~500 tokens with 50-token overlap; each chunk carries `fact_id` and `signal_id` for traceback.

Local dev uses **FAISS in-process** with the identical interface (see `level_core.memory.fakes.InMemoryVectorStore`) so nothing about the app code changes when we swap.

---

## Model layer

| Role | Model | Rationale |
|---|---|---|
| Framer, Challenger, Judge (rich) | Gemini 3.5 Pro | Complex reasoning, cited output |
| Retriever, IngestNormalizer, routing | Gemini 3.5 Flash | Fast + cheap; task is extractive |
| Voice sessions | Gemini 3.5 Flash Live | Streaming bidirectional audio |
| Embeddings | `text-embedding-004` | Cheap, accurate for our text |

All model IDs are env-configurable ([`.env.example`](./.env.example)). The Gemini client wrapper ([`level_core.models.gemini.GeminiClient`](./packages/core/src/level_core/models/gemini.py)) fronts both Vertex AI (production) and AI Studio (local dev) — chosen by `LEVEL_ENV`.

---

## Guardrails (Model Armor)

Model Armor sits at **two points**:

1. **Inbound**, on every ingested signal before it hits the vector store. Templates: `level-inbound`. Actions: PII scrubbing, prompt-injection detection, tool-poisoning block. This protects against a malicious calendar invite trying to hijack Level's agents downstream.
2. **Outbound**, on every Challenger response before it's streamed to the user. Templates: `level-outbound`. Actions: enforce the warm-adversarial tone contract, block hallucinated citations (Challenger must ground every claim in a `fact_id` — output is rejected if it references facts not in retrieval results), block leaking of another user's data.

Model Armor policy YAML lives in [`infra/model_armor/policies.yaml`](./infra/model_armor/policies.yaml).

---

## Agent Identity & Gateway

Each agent has its own **Google Cloud service account** with narrowly scoped IAM:

| Agent | SA permissions |
|---|---|
| `framer` | Vertex AI user; no Firestore write |
| `retriever` | Vertex AI user, Vertex Vector Search viewer, Firestore read-only |
| `challenger` | Vertex AI user, Firestore read (facts + manifesto) |
| `judge` | Vertex AI user, Firestore write (`bias_events` only) |
| `ingest_normalizer` | Vertex AI user, Firestore write (`facts` + `signals`), Vector Search write, Model Armor invoke |
| `conductor` | impersonate all of the above via `iam.serviceAccountTokenCreator` |

Terraform declares these SAs in [`infra/terraform/iam.tf`](./infra/terraform/iam.tf). This delivers **zero-trust between agents** — even if the Challenger were compromised, it can't write to `bias_events` or read raw signals it shouldn't.

The **Agent Gateway** ([`level_core.gateway`](./packages/core/src/level_core/gateway/)) is an in-process policy router: agents don't call tools directly, they call `gateway.invoke(tool_name, args)` which enforces per-agent allowlists and rate limits before dispatching. Every gateway call becomes an OTel span.

---

## Observability

- Every agent invocation is wrapped in a `@traced` decorator that opens an OTel span with attributes: `agent.name`, `agent.version`, `model.id`, `prompt.sha`, `tokens.input`, `tokens.output`, `latency_ms`, `retry_count`.
- Every LLM call, Firestore query, and Vector Search query is a child span.
- OTel exporter: **Cloud Trace** in cloud mode, console in local. Configured by `LEVEL_OTEL_EXPORTER`.
- Structured logs via `structlog` — JSON in cloud, pretty in local. Every log line carries `trace_id` for cross-correlation with Cloud Trace.
- `level_core.observability.audit.write_audit_event` writes append-only records to Firestore `audit/{event_id}` for every agent decision that affects user state (bias events, manifesto updates, ingestion rejections).

Combined, this satisfies the "OpenTelemetry-compliant audit logs and end-to-end reasoning chain traces" rubric requirement.

---

## Failure-mode inventory

| Failure | Detection | Recovery |
|---|---|---|
| Gemini API timeout / 5xx | httpx timeout; ADK bubbles error | Retry once with exponential backoff; on second failure, degrade turn |
| Model returns invalid JSON | Pydantic validation of `output_schema` | Retry once with `response_mime_type=application/json`; on second failure, degrade |
| Model hallucinates a `fact_id` in a citation | Post-hoc check: every `fact_id` in Challenger output must appear in Retriever output | Reject response, emit degraded turn, log to audit |
| Vertex Vector Search returns 0 hits | Retriever gets empty result set | Challenger operates on Manifesto + BiasProfile only; explicitly says "I don't have specific context on this yet" |
| Firestore write conflict | Firestore transaction retry | Automatic; capped at 3 attempts |
| Ingestion job crashes mid-batch | Cloud Run Job exits non-zero | Cloud Scheduler retries on next tick; cursor didn't advance, so no data loss |
| Model Armor rejects response | Guardrail returns `blocked=true` | Regenerate up to 2 times with tighter prompt; if still blocked, degrade |
| Agent loops (calls same tool > N times) | Gateway per-agent call count | Gateway rejects further calls, forces agent to finalize |
| User asks question with no ingested context | Retriever returns empty | Level asks a clarifying question instead of pretending |

Every recovery path emits a `degradation_event` audit record so we can watch reliability trends in Cloud Monitoring.

---

## What's explicitly deferred

For the hackathon build we're **not** shipping:
- Multi-tenant billing / plans
- SSO / SAML
- Regional deployment (single-region us-central1)
- CDN in front of the web app
- End-to-end encryption of stored signals (defer to GCP-managed encryption at rest)
- Structured feedback loop for prompt improvement (planned post-hackathon)

These are called out in [`SUBMISSION.md`](./SUBMISSION.md) as "known scope boundaries" so judges don't score us on missing enterprise features we deliberately skipped.
