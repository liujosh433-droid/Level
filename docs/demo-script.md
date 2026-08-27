# Level — demo script (target 4:00, hard cap 4:30)

> Judges watch dozens of demos. Every beat should either **prove a rubric bullet** or **land an emotional moment**. Nothing else earns its seconds.

## Pre-flight (30 minutes before recording)

- [ ] `LEVEL_DEMO_IN_CLOUD=true` set on the deployed API (verify: `curl https://<api>/v1/config/features | jq .demo.available` → `true`)
- [ ] Hard-refresh the hosted URL so the cached `/v1/config/features` isn't stale
- [ ] Warm the demo user: click **Try demo: Solo caregiver** once, then log out. This pre-populates Firestore + warms the summary cache so live click-through is fast
- [ ] Set your machine's time to **Thu Aug 27 2026** if the ICS anchor drifts — the demo week is Aug 24–30 2026
- [ ] Close every other tab, mute Slack, run a `caffeinate -d` on macOS
- [ ] Record at 1440×900, 60fps if the tool allows — cursor motion sells more than resolution
- [ ] Have `/admin/traces` open in a second Chrome window (pre-authed) for the "under the hood" beat
- [ ] Backup screen recordings of Veo + Lyria in case Vertex is slow ([Veo B-roll shot list](#b-roll-shot-list))

## Timeline at a glance

| # | Beat | Duration | Rubric bucket |
|---|---|---|---|
| 0 | Cold open — "the 3pm text chain" | 0:25 | Innovation & Utility (framing) |
| 1 | 10-second demo login | 0:20 | Demo Readiness |
| 2 | Level noticed while you slept | 0:35 | Innovation (autonomous action) |
| 3 | Chat — priority-aware booking | 0:45 | Innovation (asks clarifying qs) + Architecture (fast-path) |
| 4 | The feedback loop | 0:40 | Innovation (adapts) + Architecture (audit trail) |
| 5 | Email with human-in-the-loop | 0:35 | Innovation (mutates external systems) + Architecture (safety) |
| 6 | Voice — Hear my day | 0:20 | Bonus (Lyria) + Innovation (voice) |
| 7 | Under the hood — traces + registry | 0:30 | Architecture (30%) |
| 8 | Bonus models, earned | 0:15 | Bonus (up to 0.6 pts) |
| 9 | Close + CTA | 0:15 | — |

**Total ~4:00.** Keeping each beat under-budget by 5s gives you a 30s cushion for the actual model roundtrips.

---

## Beat 0 — Cold open (0:00 → 0:25)

**[SCREEN]** Full-frame Google Calendar week view. Real calendar, real chaos: a co-parent, a kid's soccer at 4pm, a "Mom PT?" event with a `?`. Zoom slowly toward a Tuesday afternoon block.

**[VO]**
> If you've ever been the person who runs a family's calendar, you know the moment. It's Tuesday, 2:47pm. Your kid has soccer at 4:00. You're in a meeting. And somewhere in your head, a small alarm goes off: *wait — are the shoes in the car?*
>
> Level is what would have caught that.

**[SUPER]** `Level · a caregiver's second set of hands · #AllThingsAgenticHackathon`

**[TRANSITION]** Cut to Level's landing page.

---

## Beat 1 — 10-second demo login (0:25 → 0:45)

**[SCREEN]**
1. Level landing page. Cursor hovers over the **Try demo: Solo caregiver** button.
2. Click. Full-page load; land on `/today` with a populated agenda, people chips, and the sidebar filled in.
3. Highlight the "Demo mode" pill in the top nav.

**[VO]**
> No OAuth, no Google Cloud project, no waiting. One click seeds a caregiver — Josh, two kids, an elder mom — and 200+ events pre-classified. Everything you're about to see runs against our production Gemini quota, live.

**[SUPER]** *(bottom third)* `POST /v1/auth/demo · signed cookie · seeded from example-data/`

**[RUBRIC]** *Demo Readiness*: judges see the app work in under 20 seconds.

---

## Beat 2 — Level noticed while you slept (0:45 → 1:20)

**[SCREEN]**
1. Scroll to the **"Level noticed while you slept"** section. Two proactive cards visible.
2. Zoom on card #1: *"Nova's ballet is missing this week. Want me to put it back?"* with `Nova ballet · Sports · Thu Aug 27`.
3. Scroll to **"This week's missing"**: 2 rows for solo scenario — Nova ballet (Thu), Helen weekly grocery drop (Sun).
4. Hover an event on the agenda: "Nova ballet class" (past week) — show the tooltip / activity chip = **Sports**.
5. Hover a second past occurrence: "Ballet - Nova" (different wording, same event). Show it also tagged Sports.

**[VO]**
> Level's nightly job noticed two things overnight. Nova has ballet every Thursday — but not this Thursday. Helen has a Sunday grocery drop — but not this Sunday. Neither is on the calendar.
>
> And here's the interesting part — Nova's ballet doesn't repeat on Google Calendar. The past weeks say *"Nova ballet"*, then *"Ballet - Nova"*, then *"Nova ballet class"*. Different wording every time. Level clustered them anyway — same person, same weekday, same hour band — and majority-voted the name.

**[SUPER]** *(callout)* `Usuals clustered from messy events · docs/STATE_AND_LIFECYCLE.md § 2.4`

**[RUBRIC]** *Innovation (autonomous action)*: proactive cards are a real background write, not a chat trigger.

---

## Beat 3 — Priority-aware booking (1:20 → 2:05)

**[SCREEN]**
1. Focus the chat box. Type: *"prioritize elder care over sports this week"*.
2. Reply lands in ~1s (fast-path, no LLM). Show the reply + the three feedback chips underneath.
3. In the sidebar, the **Priorities** section updates live.
4. Type: *"when's the best time to book team dinner this week?"*.
5. Reply lands with 3–4 slots, all in the 5–8pm dinner window. **No lunch or afternoon times.**
6. If a slot conflicts with an evening care event, the reply includes: *"This would conflict with these priorities: elder care with Helen"* — with **"elder care with Helen"** highlighted teal.

**[VO]**
> Priorities are one line of text. Level parsed it, wrote it to Firestore, and — critically — never called the LLM. That's a regex fast-path. In our test corpus, about 60% of chat turns never touch Gemini at all.
>
> Now — dinner. Level knows what dinner *is*. It knows Sunday, 7am is not dinner. It also knows a priority I set 20 seconds ago should shape what it suggests. And when a slot conflicts with elder care, it says so — in teal.

**[SUPER]** *(callout)* `Fast-path: /v1/admin/intents · Router+inline extraction: 2 LLM calls → 1`

**[RUBRIC]**
- *Innovation*: asks clarifying questions, adapts to spoken priorities.
- *Architecture*: fast-path registry, priority-aware slot ranking, meal-window inference.

**[BACKUP LINE]** *(if booking is slow >6s)*: "This one calls the LLM because the user's phrasing is novel — and you'll see it in the trace waterfall in a minute."

---

## Beat 4 — The feedback loop (2:05 → 2:45)

**[SCREEN]**
1. On one of Level's booking suggestions, click **Not me**. Reply below: *"Removed — I won't propose that again."*
2. Open the data sidebar wider. Highlight `memory_bank` — a new entry appears with `tag: avoid`.
3. Type: *"actually alex isn't a coparent anymore"*.
4. Reply lands: person edit confirmed. Sidebar's **People** list updates: Alex is removed / soft-deleted.
5. Open the second Chrome window on `/admin/traces`. Highlight the last two rows:
   - **Row A**: `EmailAgent` or `RouterAgent`, an audit_id like `abc123`.
   - **Row B**: `FeedbackChip`, `parent_audit_id=abc123`, with the linked memory write.

**[VO]**
> Every AI-authored artifact in this app carries three chips — Keep, Adjust, Not me. When you click one, a *second* audit row is written that points back at the original. That's the feedback loop the judging rubric asks about — and it's traceable end-to-end.
>
> The rejection I just clicked is now a memory tagged `avoid`. The next generator agent's prompt will include it as anti-example. This is not a mockup — it's the actual data flow.

**[SUPER]** *(callout)* `parent_audit_id chain · Memory Bank tag=avoid · 40-entry LRU`

**[RUBRIC]**
- *Innovation (Collaborative Partner bullet)*: constantly adapts to user corrections.
- *Architecture*: audit rows carry parent_id + signed Agent Identity token.

---

## Beat 5 — Email with human-in-the-loop (2:45 → 3:20)

**[SCREEN]**
1. Type: *"email Ms. Anna that Nova is sick tomorrow"*. (Nova is a real seeded kid in both demo scenarios — don't use a name that isn't in the roster or the agent will faithfully echo it back.)
2. Reply lands: *"Drafting email — one moment..."* (active "drafting" indicator, not a silent spinner).
3. Draft appears — full to/subject/body with the school context filled in.
4. Edit one line inline in the draft ("recovering from the flu" → "recovering"). Click **Send**.
5. Small toast: *"Preview only — Gmail send is disabled in demo mode."*
6. Cut to `/admin/traces`. Show the `EmailAgent` row with `fallback_used=null`, `turns_taken=1`, `agent_identity_token=<hash>`, and next to it the confirmation-token row.

**[VO]**
> The draft came from an EmailAgent — Pro tier, three refinement turns available, only used one. Every send goes through a confirmation token — idempotent, TTL'd, only dropped after Google confirms success. So a dying Cloud Run instance mid-send doesn't lose the draft, and clicking twice doesn't send twice.
>
> In demo mode we short-circuit before Gmail so we don't actually send. Every other guardrail is real.

**[SUPER]** *(callout)* `EmailAgent · max_turns=3 · confirmation_token TTL · human-in-the-loop`

**[RUBRIC]**
- *Innovation*: mutates external systems (Gmail) with user consent.
- *Architecture*: idempotency, HITL, signed audit rows.

---

## Beat 6 — Voice: Hear my day (3:20 → 3:40)

**[SCREEN]**
1. Click the **Hear my day** button in the chat header.
2. A soft chime plays (2–3 seconds).
3. TTS voice reads the day summary out loud. Show the caption text below.

**[VO]** (over the chime)
> That chime is Lyria, Google's music model. It plays over the summary from a Pro-tier agent, spoken via the browser's TTS. The whole flow is under two seconds cold, and cached to milliseconds on the next click.

**[SUPER]** *(callout)* `Lyria (Vertex Interactions API) → base64 chime → Audio() · SummaryAgent max_turns=1 · fingerprint cache`

**[RUBRIC]** *Bonus*: Lyria integration is real, not sprinkled.

**[BACKUP LINE]** *(if chime fails)*: "The chime is a bonus — the summary is the point. Level cached this response earlier, so it's coming back in about 50 milliseconds."

---

## Beat 7 — Under the hood (3:40 → 4:10)

**[SCREEN]**
1. `/admin/traces` full-screen. Point at the trace waterfall — multiple agents grouped by `trace_id`, with parent→child edges rendered.
2. Cut to `/v1/admin/agents` (JSON). Zoom on the array: 11 agents, each with `name`, `model`, `safety_class`, `cost_tier`, `version`, `schema`, `tools`.
3. Cut to `/v1/admin/intents`. Show the fast-path handlers with example utterances.
4. Cut briefly to `docs/architecture.png` (the Mermaid render). Point at the six planes.

**[VO]**
> Every LLM the system talks to appears at `/v1/admin/agents`. Every regex handler appears at `/v1/admin/intents`. Every call is signed with an HMAC agent-identity token — you can hand-verify it at `/v1/admin/agents/verify`. Six guardrails run before any token hits Gemini: Model Armor, rate + cost gate, PII strip, anti-injection fence, source_span echo-back, and signed identity. A circuit breaker isolates a bad Google Calendar backend per user. A three-tier model ladder — Gemini 3.5 → 2.5 → Gemma — keeps chat alive during quota outages.

**[SUPER]** *(callout, small)* `docs/architecture.mmd · docs/STATE_AND_LIFECYCLE.md · signed AgentIdentity`

**[RUBRIC]** *Architecture (30%)*: discoverable surface, layered guardrails, isolation.

---

## Beat 8 — Bonus models, earned (4:10 → 4:25)

**[SCREEN]** Split-screen or fast cuts:
- Veo weekly recap video (auto-plays for 3 seconds).
- Lyria chime waveform.
- Trace waterfall row with `fallback_used="gemma-3-4b-it"`.
- ADK `ADKPlannerAgent` row.

**[VO]**
> Four bonus points earned by working, not sprinkled: **ADK** on the email hot path, **Gemma** as a real failover in the trace waterfall, **Veo 3** for a weekly recap, **Lyria** for the chime you just heard.

**[SUPER]** `ADK · Gemma · Veo 3 · Lyria — all in the trace waterfall`

**[RUBRIC]** *Bonus contributions* (up to 0.6 pts on the rubric).

---

## Beat 9 — Close (4:25 → 4:40)

**[SCREEN]** Full-frame text card:
```
Level
github.com/liujosh433-droid/Level
Live demo · one click, no signup
Built with Gemini 3.5 · ADK · Cloud Run · Firestore
#AllThingsAgenticHackathon
```

**[VO]**
> Level. Repo's public, live demo is one click, and every claim in this video has a file path in the codebase to back it up. Thanks for watching.

---

## Recovery lines (memorize these)

| If this fails mid-take | Say this |
|---|---|
| Chat reply is >8s slow | *"This one calls the LLM — you'll see it in the trace waterfall shortly."* |
| Lyria chime doesn't play | *"Chime is a bonus — Level degrades gracefully when a Vertex model isn't enabled, and the summary still plays."* |
| Veo endpoint 404 | *"We ship recorded footage for the recap in case a Vertex model is warming up — it's cached per week."* Cut to B-roll. |
| Traces page shows an old trace | *"That's the trace from the earlier click — the timestamps prove Level really is running this live."* |
| Demo button 404s (cloud demo off) | Cut to local demo B-roll immediately, no on-camera panic. |
| Feedback chip doesn't visibly re-influence | *"Adaptation shows on the next generator turn — one more click and you'd see the memory in the recall block."* |

---

## B-roll shot list (pre-record these once, keep as fallback footage)

- Landing page → demo-button click → `/today` populated. **10 seconds.**
- Chat: *"prioritize elder care over sports"* + reply + sidebar update. **6 seconds.**
- Chat: *"when's the best time to book team dinner this week?"* + slot list. **8 seconds.**
- Feedback chip **Not me** click + memory_bank sidebar update. **5 seconds.**
- Email draft render + one edit + Send click + demo-mode toast. **12 seconds.**
- Hear my day: chime + TTS. **6 seconds.**
- `/admin/traces` waterfall with parent→child edges. **8 seconds.**
- `/v1/admin/agents` JSON scroll. **4 seconds.**
- Veo weekly recap (full 15-second clip in one take). **15 seconds.**
- Architecture diagram slow pan across the six planes. **8 seconds.**

Keep these in a folder called `demo-broll/` on the recording machine. Every one is a safety net.

---

## Voiceover pacing notes

- Target **140–150 wpm**. The script above is ~600 words for 4:00; that's ~150 wpm. Rehearse to that pace.
- Pause **one beat** after the framing lines in Beat 0. Let the calendar chaos land visually.
- **Never read a URL out loud.** They live in supers.
- **Never read a file path out loud.** Same reason.
- Say "Level" no more than 5 times total. Overuse feels like an ad.
- End sentences on downbeats — makes it sound authoritative, not chirpy.

---

## What we deliberately don't show

We can't fit everything in 4 minutes. These are cut on purpose, not by accident. If a judge asks in the follow-up:

- **Circuit breaker** — mentioned in Beat 7 VO; live-visible at `/v1/admin/calendar_circuit`.
- **Model Armor** — mentioned in Beat 7 VO; unit-tested in `tests/security/`.
- **PII scrub** — mentioned in Beat 7 VO; unit-tested.
- **Rate limit** — mentioned in Beat 7 VO; state at `/v1/admin/rate_limit`.
- **Nightly Cloud Run Job** — implicit in Beat 2's "while you slept" framing; source in `packages/jobs/`.
- **Signed sessions + Firestore rules** — implicit in Beat 1's demo-login pill.

Every one of these is either in the repo, in `SUBMISSION.md`, or in the medium writeup at `docs/writeup-medium.md`. The demo video's job is to make the judge *want* to open those links.
