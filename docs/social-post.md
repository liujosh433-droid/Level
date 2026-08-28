# Social post drafts

Both drafts already include the required `#AllThingsAgenticHackathon`
hashtag and hackathon disclosure. Post before the submission deadline.

## X / Twitter (thread)

**1/**
> Shipped Level for #AllThingsAgenticHackathon: a caregiver partner
> that reads your Google Calendar, learns your family's usual rhythm,
> and nudges you when something's missing — like the soccer shoes you
> keep forgetting.
>
> Built on Gemini 3.5 + ADK + Cloud Run + Firestore.

**2/**
> Three design bets:
>
> • Loop closes on the *user*: Keep/Adjust/Not-me chips write to a
>   Memory Bank + Negatives that flow back as few-shot on the next
>   call. Adapts without fine-tune.
> • O(1) rate/cost gate via a hot counter (was O(N) over ai_audit).
> • ADK on the hot path, not the shelf.

**3/**
> Model soup:
>
> • Gemini 3.5 Flash + Pro (primary)
> • Vertex 2.5 (tier-2 fallback on 429)
> • Gemma via Vertex Model Garden (tier-3 extraction fallback)
> • Veo 3 for an 8-second Info-page film
> • Lyria for the "Hear my day" chime
>
> Every fallback is visible in /admin/traces.

**4/**
> Demo: [YOUTUBE]
> Writeup: [DEVTO-LINK]
> Code: [GITHUB]
>
> #AllThingsAgenticHackathon

## LinkedIn

> Just finished my submission for the All Things Agentic Hackathon:
> **Level**, a caregiver's second set of hands.
>
> The problem: a busy caregiver's Google Calendar carries pickups,
> therapy, sports, and school events for 2–3 people in parallel with
> their own life. When something slips the cost is a 3pm text chain.
>
> Level watches the calendar and speaks up gently when something is
> missing. It also drafts the email, blocks the conflict, and
> remembers the correction the next time.
>
> A few design bets worth calling out:
>
> — **The loop closes on the user, not the model.** Every AI-authored
> artifact carries Keep / Adjust / Not-me chips. Keep persists to a
> Memory Bank; Adjust and Not-me write few-shot negatives. The
> generator agents (email, day summary) start sounding like the
> caregiver after ~3 chats.
>
> — **Guardrails belong in code, not the prompt.** Model Armor
> (deterministic prompt-injection prefilter), signed Agent Identity
> in every audit row, `source_span` hallucination guard, PII scrub
> before every call, human-in-the-loop for every external mutation,
> O(1) rate/cost gate, tiered fallback ending in Gemma.
>
> — **ADK is on the hot path.** `LEVEL_ADK_MODE=true` routes email +
> book intents through a `google.adk.LlmAgent`. Planner audit rows
> carry a parent_audit_id so /admin/traces renders a real waterfall.
>
> Built on Gemini 3.5 (Pro + Flash), Google ADK, Cloud Run, Cloud Run
> Jobs, Firestore, Vertex AI, Cloud Trace, Cloud Scheduler, plus
> Gemma, Veo 3, and Lyria as bonus Google models.
>
> #AllThingsAgenticHackathon
>
> Demo: [YOUTUBE]
> Writeup: [DEVTO-LINK]
> Code: [GITHUB]
