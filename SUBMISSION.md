# Level - All Things Agentic Hackathon submission

**Track:** Collaborative Partner
**Live demo:** _fill me in after deploy_
**Repo:** _fill me in with your GitHub URL_
**Demo video (YouTube):** _fill me in after recording_
**Judge access:** `testing@devpost.com` and `cloudhackathons@google.com` are
listed as OAuth test users and (if the repo is private) invited as GitHub
collaborators.

## What it is

A caregiver partner for busy parents and multi-generational households.
Level reads your Google Calendar, learns which humans you care for, notices
your usual weekly rhythm, tracks your priorities, drafts school emails, and
speaks a short summary of your day when your hands are full.

## Mandatory stack (checklist)

| Requirement | Where |
|---|---|
| Gemini 3.5 (or newer) via Vertex AI | [`packages/core/src/level_core/agents/base.py`](packages/core/src/level_core/agents/base.py) `_invoke_vertex()` |
| Google Agent Development Kit (ADK) | [`packages/core/src/level_core/agents/adk_tools.py`](packages/core/src/level_core/agents/adk_tools.py) `build_level_agent()` |
| Google Cloud infrastructure | Cloud Run API, Cloud Run Job, Firestore, Vertex AI, Gmail, Calendar, Secret Manager, Cloud Trace, Cloud Scheduler - all provisioned in [`infra/terraform`](infra/terraform) |

## Agents

| Agent | Model | Purpose |
|---|---|---|
| [`ChatRouterAgent`](packages/core/src/level_core/agents/chat_router.py) | flash | Classify chat message into path + intent |
| [`RoleAgent`](packages/core/src/level_core/agents/role.py) | pro | Propose care_people from calendar rollup |
| [`UsualAgent`](packages/core/src/level_core/agents/usual.py) | flash | Disambiguate tied weekly patterns |
| [`ActivityAgent`](packages/core/src/level_core/agents/activity.py) | flash | Assign activity_type to unseen events (cached forever per event) |
| [`PriorityAgent`](packages/core/src/level_core/agents/priority.py) | pro | Structured extract of chat-stated priorities |
| [`ReminderAgent`](packages/core/src/level_core/agents/reminder.py) | flash | Structured extract of chat reminders (person + activity) |
| [`EmailAgent`](packages/core/src/level_core/agents/email.py) | flash | Draft school-style email (human-in-the-loop send) |
| [`SummaryAgent`](packages/core/src/level_core/agents/summary.py) | flash | 2-3 sentence Hear-my-day summary |

Every call goes through [`call_agent()`](packages/core/src/level_core/agents/base.py)
which enforces Pydantic structured output, `<user_input>` fence,
`source_span` echo-back hallucination guard, retry+backoff, per-user rate
limit, daily cost cap, and writes an
[`AiAuditEntry`](packages/core/src/level_core/schemas/audit.py).

## Rubric mapping

### Innovation & Operational Utility (40%)

- **Actively mutates data**: Level writes calendar events with a private
  `origin=level` tag ([`schedule/book.py`](packages/core/src/level_core/schedule/book.py))
  and sends Gmail messages ([`email/gmail_client.py`](packages/core/src/level_core/email/gmail_client.py)).
- **Messy unstructured input**: caregiver calendars are the target -
  first-name attendee tokens, ambiguous summaries, weekly patterns that
  break during school holidays.
- **Adapts to the user**: Not-me clicks land in
  [`negatives/`](packages/core/src/level_core/schemas/negative.py) and get
  injected into the *next* agent prompt as few-shot "do not propose this
  again." No fine-tuning required.

### Architectural Discipline & Tech Stack (30%)

- **Separation of concerns**: 7 single-purpose agents. Extraction agents
  run at `temperature=0` and cap at 1 turn; generative agents (Email,
  Summary) at 0.4 and cap at 3 turns.
- **State management**: dual storage backend
  ([`storage/factory.py`](packages/core/src/level_core/storage/factory.py))
  with the same repo interface over local JSON or Firestore. Optimistic
  concurrency via `version` field.
- **Failure tolerance**: schema failure returns the agent's safe default
  and logs `hallucinated=true`; `source_span` mismatch drops individual
  fields; 3x retry with exponential backoff on 429/500; gate drops
  non-chat AI when daily cost cap is hit.
- **Failure isolation**: Gmail send and Calendar write both require a
  `confirmation_token` returned by the preceding draft/find call - no
  agent can autonomously mutate external state.

### Demo & Production Readiness (30%)

- **Architecture diagram**: [`docs/architecture.png`](docs/architecture.png)
  (source [`docs/architecture.mmd`](docs/architecture.mmd)).
- **Reproducible setup**: [SETUP.md](SETUP.md) has both local and cloud
  paths; `make demo-seed` gives judges a populated UI without needing a
  real calendar.
- **Proof of action**: [/admin/traces](apps/web/src/app/(dashboard)/admin/traces/page.tsx)
  is a live agent trace view refreshing every 3 seconds - the demo video
  uses it to show real Gemini calls happening.
- **Google Cloud visible**: video shows the Cloud Run URL,
  `gcloud run services logs read`, and Firestore console mutations.

## Bonus contributions (+ up to 1.0)

- **+0.2** Gemma via Vertex as a fallback classifier when Gemini quota is
  exhausted. Set `LEVEL_MODEL_GEMMA=gemma-3-4b-it`; falls in
  [`agents/activity.py`](packages/core/src/level_core/agents/activity.py).
- **+0.2** dev.to writeup: `docs/writeup-devto.md` (draft included -
  publish before the deadline with the `#AllThingsAgenticHackathon` tag).
- **+0.2** Social post: `docs/social-post.md` (X / LinkedIn draft with the
  required hashtag).

## Demo video plan (<= 4 min)

Scene-by-scene script in
[the rebuild plan](.cursor/plans/rebuild_level_c285e8fa.plan.md) section
4.1. Highlights:

1. Connect Google -> Firestore fills with `agenda_cache/` docs (Proof of
   action #1: unedited).
2. `/profile` shows AI-proposed people + usuals; click Not-me and watch
   `/admin/traces` log the negative + next `RoleAgent` call skipping it
   (feedback loop demo).
3. Chat "book me gym Tuesday morning" -> streaming reply -> confirm ->
   Google Calendar shows new event tagged `origin=level` (Proof of action
   #2: data mutation).
4. Chat "I forgot Beta's soccer shoes" -> reminder appears on today's
   soccer event as a chip (structured match demo).
5. Contacts -> "Draft email to Ms. Rivera, sick today" -> edit -> send ->
   Gmail Sent (Proof of action #3).
6. "Hear my day" voice; Cloud Run logs + Cloud Trace showing full agent
   chain in one trace.

## Data privacy notes

- Raw calendar event descriptions never leave the API. Only stable
  first-name tokens make it into `agenda_cache.attendee_tokens`.
- Emails, phone numbers, and street addresses are stripped from every
  prompt via [`agents/pii.py`](packages/core/src/level_core/agents/pii.py).
- OAuth secrets live in Secret Manager, mounted as env vars at Cloud Run
  runtime.
- `DELETE /v1/me` wipes the entire per-user Firestore subtree and revokes
  the Google token.
