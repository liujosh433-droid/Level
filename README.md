
# Note
I say "we" even though it's really just me and my 2 Github accounts (different machines sometimes) and to foster a more inclusive tone :)) 

# Level

Caregiver partner for busy parents and multi-generational households. Level
reads your Google Calendar, learns which humans you care for, notices your
usual weekly rhythm, tracks your priorities, drafts school emails, and speaks
a short summary of your day.

Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/)
in the **Collaborative Partner** track.

---

## Try it, watch it, read it

- **Live demo**: https://level-web-185318998255.us-central1.run.app/today
  Click **Try demo: Solo caregiver** on the landing page. Nothing to install,
  no API key on your end.
- **Video walkthrough**: https://www.youtube.com/watch?v=ETJRg-02_0s
  3-minute demo of the full loop: connect → today → chat → draft email → hear my day.
- **Write-up**: https://medium.com/@liujosh433/building-level-a-caregivers-second-set-of-hands-2bec3ddabdab

  What we built, why we built it this way, and what surprised us.

---

## What Level does

- **Reads/learns your calendar, per-person.** Google Calendar delta sync fills a
  cache; the RoleAgent proposes who each event is about; a name-vs-noun guard
  helps to identify people/care-roles.

- **Learns your weekly rhythm.** Four weeks of history → majority-vote
  usuals like *"Nova ballet, Thu 4:30–5:30pm"*. Handles messy calendars where
  the same event is titled three different ways.

- **Notices what's missing.** A nightly job flags usuals that didn't happen
  this week. On `/today` they surface as **"Level noticed while you slept"**
  cards with a one-tap "put it back."
  
- **Chat that gets things done.** Fast-paths handle greetings, priorities,
  bookings, reminders, and agenda lookups in <10 ms with zero LLM cost.
  Everything else routes through a Gemini 3.5 Flash router, then a specialist
  agent (Email / Book / Person / Summary / …). Streams back over SSE.

  - **Attach reminders/notes to events**
  Simply tell Level "Remind me to bring my charger to work" and it'll attach that
  note onto all work-related events. 

- **Drafts school emails.** Gmail send is behind a confirmation token + 10 min
  TTL; demo mode short-circuits to a preview so nothing ever leaves your
  inbox by accident.

- **"Hear my day"** in one tap. SummaryAgent → Web Speech TTS, with an
  optional Lyria calm/hopeful/energetic chime intro.

- **Feedback closes the loop.** Every AI artifact has keep / adjust / not-me
  chips. Kept facts land in Memory Bank; rejections become few-shot
  negatives on the next matching agent call.

- **Traceable end-to-end.** Every LLM call carries an HMAC-signed
  `agent_identity` + `parent_audit_id`. `/admin/traces` renders a real
  waterfall grouped by `trace_id`.

### Google stack we're using

**Required (Collaborative Partner track):**

- **Gemini 3.5 Pro + Flash** — every LLM call goes through
  `agents/base.py::call_agent`. Flash for the router + extractors, Pro
  for generators.
- **Google Agent Development Kit (ADK)** — hot-path planner. Set
  `LEVEL_ADK_MODE=true` and every email + booking intent is planned by a
  `google.adk.LlmAgent`. Planner audit rows carry `parent_audit_id` so
  `/admin/traces` renders a real waterfall
  (`agents/adk_runner.py`).
- **Google Cloud** — Cloud Run (API), Cloud Run Jobs (nightly),
  Firestore (state), Vertex AI (model host), Secret Manager, Cloud Trace
  (OpenTelemetry), Cloud Scheduler, Gmail API, Calendar API.

**Bonus models integrated for extra credit:**

- **Gemma 3** as a tier-3 extraction fallback when both AI Studio 3.5 and
  Vertex 2.5 return 429. Extractor agents (RoleAgent, ActivityAgent,
  UsualAgent, …) keep working through quota storms
  (`agents/invoke.py::_try_gemma`).
- **Veo 3** generates the 8-second Info-page film on `/about`. Generated
  once, cached in GCS, reused forever
  (`api/routes/media.py::about_intro`). Enable with
  `LEVEL_MEDIA_ENABLED=true`.
- **Lyria** generates calm / hopeful / energetic chime intros for
  "Hear my day," cached per mood
  (`api/routes/media.py::daily_chime`).

Full agent list, guardrails, and hackathon rubric mapping in
[SUBMISSION.md](SUBMISSION.md).

---

## Local setup (2 minutes, no GCP, no OAuth)

Prereqs: `node >= 20`, `python >= 3.12`, and [`uv`](https://docs.astral.sh/uv/).
Install `uv` with `brew install uv` or
`curl -LsSf https://astral.sh/uv/install.sh | sh`.

**Step 1 — Grab a free Gemini API key** at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).
No credit card, no GCP project. Free tier (~15 req/min, ~1M tokens/day on
`gemini-2.5-flash`) is plenty for a judging session. Without a key the app
still runs — every LLM path degrades to a deterministic template (see
[SETUP.md § Skipping the key](SETUP.md#skipping-the-key-what-you-lose)).

**Step 2 — Bring it up.**

```bash
cp .env.example .env
# Paste your key into the GOOGLE_API_KEY= line. Everything else can stay
# at defaults — GOOGLE_OAUTH_* stays blank for demo mode.
make install
make dev
# API on http://127.0.0.1:8080, web on http://127.0.0.1:3000
```

**Step 3 — Click Try demo.** Open `http://127.0.0.1:3000` and click
**Try demo: Solo caregiver**. Level seeds a synthetic user from
[`example-data/caregiver-month-solo.ics`](example-data/caregiver-month-solo.ics),
drops the same signed session cookie a real OAuth callback would, and lands
you on `/today` with 200+ pre-classified events, a curated cast of people,
and missing-usuals for the current week already computed.

Two scenarios are available:

- **Solo caregiver** — the primary demo. Single parent (Josh) with two kids
  and elder care for Helen. RoleAgent inference story lands hardest here.
- **Two-parent family** — adds co-parent Alex so you can see how Level
  handles schedules with two adults.

Everything else — real OAuth setup, cloud deploy, Veo/Lyria wiring, port
collisions with Cursor — lives in [SETUP.md](SETUP.md).

### Test

```bash
make test          # unit + security + e2e (all offline)
make test-cov      # enforces 75% on packages/api (85% target on packages/core)
make test-e2e-web  # Playwright smoke against local dev
```

---

## Architecture

![architecture](docs/architecture.png)

Diagram source: [`docs/architecture.mmd`](docs/architecture.mmd). Regenerate
with `make diagram`.

### The request path

```
Caregiver → Next.js /today
         → FastAPI /v1/chat (or /v1/chat/stream SSE)
             → Model Armor (prompt-injection prefilter, deterministic)
             → O(1) rate + cost gate (hot counter, not audit scan)
             → strip_pii + <user_input> fence
             → call_agent (ChatRouter → specialist)
                 → Gemini 3.5 Flash / Pro
                    ↓ 429
                 → Gemini 2.5 (tier-2)
                    ↓ 429
                 → Gemma 3 (tier-3, extractors only)
             → source_span hallucination guard → SSE stream out
             → ai_audit row with signed agent_identity
```

### State model

Level is a **per-user document graph** with dumb CRUD repos and one KV per
user. There is no cross-user query surface.

```
UserStore                              cloud backing (Firestore)
├── people             → users/{uid}/care_people/{id}
├── usuals             → users/{uid}/usuals/{id}
├── priorities         → users/{uid}/priorities/{id}
├── reminders          → users/{uid}/reminders/{id}
├── contacts           → users/{uid}/contacts/{id}
├── agenda             → users/{uid}/agenda_cache/{id}
├── daily_agenda      → users/{uid}/daily_agenda/{id}
├── chat_turns         → users/{uid}/chat_turns/{id}
├── negatives          → users/{uid}/negatives/{id}
├── ai_audit           → users/{uid}/ai_audit/{id}
└── KV slots           → users/{uid}/state/{profile,calendar_sync,google_oauth}
```

Two backends implement `UserStore` behind one interface: `local` (JSON on
disk with per-file `asyncio.Lock`) and `cloud` (Firestore Native Mode with
transactional `KVStore` writes). Backend selection lives in one file
(`storage/factory.py`); feature code never branches on env.

### Lifecycle (the parts that surprised us)

| State                     | Lifecycle |
|---------------------------|-----------|
| `agenda`                  | Delta sync via GCal `syncToken`; window is 14d back + 28d forward; 410 falls back to full pull. |
| `chat_turns`              | Trimmed to last 20 per user by the nightly job. Long-lived context lives in Memory Bank, not here. |
| `ai_audit`                | 30-day TTL enforced by nightly job. `/admin/traces` reads the most recent 50–100 rows. |
| `negatives`               | No TTL; small rows fed back as few-shot on the next matching agent call (capped at 20). |
| `profile["memory_bank"]`  | Written on `verdict=keep`; capped at 40 per user (LRU by `last_used_at`); `avoid` tag splits positive vs. anti-example. |
| `profile["_gate_counters"]` | Hot counter for the rate + cost gate. Auto-rolls on hour + day boundaries. Bootstrap path backfills from `ai_audit` on first read. |
| `profile["proactive_cards"]` | Regenerated nightly. Only the current ISO week's cards are surfaced. Dismissal is per-card, per-week. |
| `tokens`                  | Firestore-native encryption; refresh tokens rotate transparently; wiped by `DELETE /v1/me`. |
| Demo mode                 | `POST /v1/auth/demo` resets the whole per-user subtree first (`store.reset_all()` = one `recursive_delete` call), then re-seeds. Every click is a clean slate. |

The full walkthrough of security, scalability, performance, and every
"why we did it this way" tradeoff is in
[docs/STATE_AND_LIFECYCLE.md](docs/STATE_AND_LIFECYCLE.md).

### What scales, what doesn't

- **Trivially horizontal**: no cross-user state, so 10× users is 10×
  independent document graphs. Firestore + Cloud Run both scale linearly.
- **O(1) hot paths**: rate + cost gate reads one counter doc, not the
  full `ai_audit` history (~500× fewer Firestore reads per turn).
- **Delta sync + diff writes**: rescans send only changed events; writes
  are keyed by etag so a no-op sync does 0 writes.
- **First bottleneck**: `nightly.py::_list_users` does a
  `collection("users").stream()`. Fine for hackathon scale; at >1M users
  we'd shard by uid-hash or move to Pub/Sub fan-out.

---

## Where the code lives

| Concept                             | Path |
|-------------------------------------|------|
| **Request path** |  |
| FastAPI routers                     | `packages/api/src/level_api/routes/` |
| Chat + SSE stream                   | `packages/api/src/level_api/routes/chat.py::chat`, `chat_stream` |
| Fast-path registry                  | `packages/api/src/level_api/routes/_fast_path_registry.py` |
| Per-request memoized store          | `packages/api/src/level_api/routes/_chat_context.py` |
| HTTP rate limit (token bucket)      | `packages/api/src/level_api/rate_limit.py` |
| **Agent runtime** |  |
| Agent registry                      | `packages/core/src/level_core/agents/registry.py` |
| Agent identity signing (HMAC)       | `packages/core/src/level_core/agents/identity.py` |
| Model Armor prefilter               | `packages/core/src/level_core/agents/model_armor.py` |
| Memory Bank                         | `packages/core/src/level_core/agents/memory_bank.py` |
| O(1) rate + cost gate               | `packages/core/src/level_core/agents/gate.py` |
| Router response cache (LRU + TTL)   | `packages/core/src/level_core/agents/router_cache.py` |
| Multi-turn refinement + tiered fallback | `packages/core/src/level_core/agents/base.py::call_agent`, `agents/invoke.py::invoke_with_retry` |
| Gemma 3 tier-3 fallback             | `packages/core/src/level_core/agents/invoke.py::_try_gemma` |
| ADK hot-path planner                | `packages/core/src/level_core/agents/adk_runner.py` |
| **Calendar** |  |
| Incremental sync (syncToken + 410)  | `packages/core/src/level_core/calendar/sync.py` |
| Parallel classification (Sem 4)     | `packages/core/src/level_core/calendar/enrich.py::_classify_unseen` |
| Circuit breaker for GCal            | `packages/core/src/level_core/calendar/circuit_breaker.py` |
| Name-vs-noun guard (RoleAgent)      | `packages/core/src/level_core/calendar/person_guard.py::evaluate_proposed_name` |
| Usuals engine                       | `packages/core/src/level_core/calendar/usuals.py` |
| Proactive-cards generator           | `packages/core/src/level_core/calendar/proactive.py` |
| **Storage** |  |
| Storage factory (backend switch)    | `packages/core/src/level_core/storage/factory.py` |
| Firestore backend                   | `packages/core/src/level_core/storage/firestore.py` |
| Local JSON backend                  | `packages/core/src/level_core/storage/local_json.py` |
| **Background** |  |
| Nightly job (usuals + TTL + cards)  | `packages/jobs/src/level_jobs/nightly.py` |
| Demo seeder                         | `packages/core/src/level_core/demo/seeder.py` |
| ICS fixture generator               | `packages/jobs/src/level_jobs/make_caregiver_ics.py` |
| **Observability + admin** |  |
| Trace waterfall                     | `packages/api/src/level_api/routes/admin.py::_group_by_trace` |
| Feedback loop                       | `packages/api/src/level_api/routes/feedback.py` (write) + `agents/memory_bank.py::recall_split` (read) |
| Loop integration test               | `tests/unit/test_feedback_loop_closes.py` |
| **Media** |  |
| Veo Info-page film                  | `packages/api/src/level_api/routes/media.py::about_intro` |
| Lyria chime                         | `packages/api/src/level_api/routes/media.py::daily_chime` |
| **Frontend** |  |
| Next.js dashboard                   | `apps/web/src/app/(dashboard)/` |
| Today page + proactive cards        | `apps/web/src/app/(dashboard)/today/page.tsx` |
| **Infra** |  |
| Terraform (GCP)                     | `infra/terraform/` |
| Firestore rules (per-user isolation)| `infra/firestore.rules` |

---

## Repo layout

```
apps/web                    Next.js 15 dashboard
packages/api                FastAPI (Cloud Run)
packages/core               Domain: agents, calendar, care, schedule, email, storage, voice
packages/jobs               Cloud Run Jobs: nightly usuals + TTL + watch + proactive cards
infra/terraform             GCP resources
infra/firestore.rules       Per-user isolation
docs/architecture.mmd       Architecture diagram source
docs/STATE_AND_LIFECYCLE.md State + security + scalability + performance walkthrough
tests/unit                  Fast offline tests
tests/security              Prompt-injection corpus + auth + Firestore rules
tests/e2e                   Full API flow with LEVEL_ENV=local
```

## License

MIT — see [LICENSE](LICENSE).
