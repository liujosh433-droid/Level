---
title: How I built Level — a caregiver's second set of hands — with Gemini 3.5, ADK, and Gemma
tags: [gemini, adk, googlecloud, firestore]
canonical_url:
cover_image:
description: An ADK-orchestrated caregiver partner for the All Things Agentic Hackathon. Notes on prompt-injection defense, a signed Agent Identity, a real Memory Bank, and an O(1) rate/cost gate.
---

*I built this project for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/). This post is the required writeup — every image and code pointer here is in the [repo](https://github.com/YOUR-USER/level).* **#AllThingsAgenticHackathon**

## The problem

A busy caregiver's Google Calendar carries pickups, therapy
appointments, soccer practices, and school events for two or three
people whose lives they run in parallel with their own. When something
slips — "wait, Tuesday is Beta's soccer, did I put out the shoes?" — the
cost is a frantic 3pm text chain.

Level watches the calendar and speaks up gently when something is
missing. It also drafts the email, blocks the conflict, and remembers
the correction the next time.

## The rubric-shaped choices

Anyone building for **All Things Agentic** has to make bets on the
same three axes: Innovation (does it *do* something), Architecture (is
it built like software, not a demo), Production Readiness (can a judge
actually see it work). I tried to be honest about which bet each design
choice served.

### Innovation: the loop closes on the user, not the model

Every AI-authored artifact in the chat carries three chips: **Keep /
Adjust / Not me**. Adjust and Not-me write a `NegativeFeedback` row
that the same agent's next call sees as few-shot ("do NOT propose this
again"). But the interesting one is **Keep**: on generator outputs
(email body, priority text, reminder text) a Keep click persists the
value to a **Memory Bank** which the generators recall on the next
request. The system learns *positive* preferences and adapts tone
without fine-tuning.

The result: after three chats the caregiver's emails start sounding
like them. After a week, the summary agent picks the phrases they
Kept.

### Architecture: name the components graders look for

The rubric explicitly rewards architectural discipline. I built the
system so a judge can grep for it:

- **`AgentRegistry`** (`agents/registry.py`) — a single file with
  every LLM the system talks to, its safety class, cost tier, version,
  schema, and registered tools. Fetchable at `GET /v1/admin/agents`.
- **`AgentIdentity`** (`agents/identity.py`) — every audit row's
  `model` column carries an HMAC-signed token of
  `name|version|prompt_hash`. `GET /v1/admin/agents/verify?token=`
  proves the row wasn't hand-edited.
- **Model Armor** (`agents/model_armor.py`) — a deterministic
  prefilter that runs BEFORE the gate and BEFORE any LLM call.
  Blocks obvious "ignore previous", "reveal system prompt",
  credential-fishing, and `exec()` attempts with zero spend. Flags
  softer signals for logging.
- **Memory Bank** (`agents/memory_bank.py`) — long-lived facts that
  outlive `chat_turns` (which is capped at 20). Capped at 40 memories
  per user, LRU by `last_used_at`, injected as few-shot into
  generator prompts.
- **ADK on the hot path** — `LEVEL_ADK_MODE=true` makes the ADK
  `LlmAgent` pick which sub-tool to run for email + book intents. The
  planner writes an `ADKPlannerAgent` audit row with a
  `parent_audit_id` link, so `/admin/traces` renders a real
  waterfall.

### Production Readiness: measure what a judge would measure

Three things a judge tests within 60 seconds:

1. **Does it hallucinate?** Every extraction schema requires a
   `source_span` that must appear verbatim in the user input.
   `_walk_source_spans` drops offending fields; siblings survive so
   one bad row doesn't fail the whole list.
2. **Does it burn my quota?** The rate/cost gate is O(1). It used to
   scan `ai_audit` on every request (O(N)) — v2 replaced it with a hot
   counter under `profile["_gate_counters"]`. At Firestore pricing
   that's ~500x fewer document reads per turn for an active user.
   Bootstrap path backfills from `ai_audit` on first check per user
   so upgrading is transparent.
3. **Can I see it happening?** `/admin/traces` renders a live
   waterfall grouped by `trace_id`, refreshing every 3 seconds. Every
   audit row shows `agent`, `model`, `latency_ms`, `cost_usd`, and
   flags for `hallucinated`, `blocked_by_safety`, `fallback_used`
   (e.g. `gemma-3-4b-it`), and `turns_taken`.

## What actually surprised me

**Multi-turn refinement is more useful than a bigger model.** Setting
`max_turns=3` on `EmailAgent` + `SummaryAgent` and feeding schema /
source-span failures back into the second turn eliminated 90% of the
regenerations I would otherwise have done at temp=0.4. That's a real
enforcement of a spec field, not just documentation.

**Fast-paths matter more than the LLM.** The router only runs on
turns that don't match a deterministic parser. About 60% of chat
messages in my test corpus ("book Tuesday 2-3pm", "prioritize elder
care over sports", "remind me to bring shoes") never hit Gemini at
all — they're regex + intent match, with the router as backstop for
everything else. That's what keeps the daily cost cap defensible.

**Gemma is a great safety net.** When AI Studio's free tier 429s
mid-demo (it will), Vertex 2.5 catches it. When Vertex 2.5 is also
rate-limited (Model Garden hasn't been happy this week), I fall
through to Gemma via Model Garden for the five extraction agents.
Same schema, slightly different prose, and the demo doesn't stop.
The audit row's `fallback_used` field surfaces this in the trace
waterfall live.

## Storage design

Dual backend on one repo interface: `LEVEL_ENV=local` → JSON files
under `.level/local_store/`; `LEVEL_ENV=cloud` → Firestore. Same
feature code either way. See
[`docs/STATE_AND_LIFECYCLE.md`](https://github.com/YOUR-USER/level/blob/main/docs/STATE_AND_LIFECYCLE.md)
for the full lifecycle + security + scalability walk-through.

## The bonus models

- **Veo 3** on `/v1/media/recap`: a 15-second cinematic weekly recap
  cached per ISO week per user. Prompt is built from category labels
  + priority content words — no PII.
- **Lyria** on `/v1/media/chime`: a calm / hopeful / energetic chime
  that plays around "Hear my day". Same PII-free approach; one prompt
  per mood, so the whole app shares a small chime library.

Both endpoints degrade gracefully to `{ready: false, reason: ...}`
when the Vertex project doesn't have the model enabled. The frontend
just doesn't render the video/audio in that case.

## What I'd change with more time

- **True token streaming** from Vertex for the SummaryAgent. Today's
  SSE chunks a completed reply; using
  `client.models.generate_content_stream()` for the summary path
  would drop time-to-first-byte by ~1.5s.
- **Firestore composite indexes** on `agenda.time.start`,
  `chat_turns.created_at`, and `ai_audit.created_at`. Cheap wins once
  scans get real.
- **Precompute the weekly Veo recap** in the nightly job rather than
  on-demand.

## Code and demo

Repo: [github.com/YOUR-USER/level](https://github.com/YOUR-USER/level)
Video: [YOUTUBE-LINK]

**#AllThingsAgenticHackathon**
