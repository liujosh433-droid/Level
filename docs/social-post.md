# Social post drafts

Copy either and post before the submission deadline. Both include the
required `#AllThingsAgenticHackathon` hashtag.

## X / Twitter

> Shipped Level for #AllThingsAgenticHackathon: a caregiver partner that
> reads your Google Calendar, learns your family's usual rhythm, and
> nudges you when something's missing (like the soccer shoes you keep
> forgetting).
>
> Gemini 3.5 + Google ADK + Cloud Run + Firestore.
>
> Not-me clicks teach the agent in-session, no fine-tune required.
>
> [demo] [repo]

## LinkedIn

> Just finished my submission for the All Things Agentic Hackathon: Level,
> a caregiver's second set of hands.
>
> A few design bets worth calling out:
> - Structured everything: a shared `activity_type` enum joins usuals,
>   priorities, reminders, and agenda events - so reminder matching is
>   `(person_id, activity_type)` equality, no per-event LLM.
> - Every "Not me" click writes a negative that becomes few-shot on the
>   next agent call. The system adapts to a family shape without a
>   fine-tune.
> - Every AI call is guarded: `<user_input>` fence, `source_span`
>   hallucination check, PII scrub, structured JSON output, daily cost
>   cap, human-in-the-loop for every external mutation.
>
> Built on Gemini 3.5 + Google ADK + Cloud Run + Firestore.
>
> #AllThingsAgenticHackathon
>
> Demo: [link]
> Repo: [link]
