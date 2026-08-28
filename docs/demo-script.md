# Level — demo script (hard cap 4:00)

> Read this straight through into the mic. Every beat has one thing you're excited about + one line on how it's built. Nothing else is in here.

Target pace **150 words per minute**. The spoken VO below totals **~540 words = 3:35**, leaving **25 seconds** of pauses for the app to actually respond. If you speak faster, insert a beat after each `—`.

## Pre-flight (30 minutes before recording)

- [ ] `LEVEL_DEMO_IN_CLOUD=true` set on the deployed API (verify: `curl https://<api>/v1/config/features | jq .demo.available` → `true`)
- [ ] Hard-refresh the hosted URL so the cached `/v1/config/features` isn't stale
- [ ] Warm the summary cache: click **Try demo: Solo caregiver** once and run **Hear my day** so the first Lyria call is already amortized. (State resets on every login, so the click itself won't pollute the recording — just the summary cache.)
- [ ] Set your machine's time to **Thu Aug 27 2026** if the ICS anchor drifts (demo week is Aug 24–30 2026)
- [ ] Close every other tab, mute Slack, `caffeinate -d` on macOS
- [ ] Record at 1440×900, 60fps if the tool allows — cursor motion sells more than resolution
- [ ] Have `/admin/traces` open in a second Chrome window (pre-authed) for the "under the hood" beat
- [ ] Backup screen recordings of Veo + Lyria in case Vertex is slow (see [B-roll shot list](#b-roll-shot-list))

## Timeline

| # | Beat | End time | Rubric bucket |
|---|---|---|---|
| 0 | Cold open — the mental load | 0:24 | Innovation & Utility (framing) |
| 1 | 10-second demo login | 0:36 | Demo Readiness |
| 2 | Proactive noticing on messy calendars | 1:12 | Innovation (autonomous) + Architecture (nightly job) |
| 3 | Priority-aware, sub-second booking | 1:48 | Innovation (clarifying qs) + Architecture (fast-path) |
| 4 | Feedback loop that actually learns | 2:18 | Innovation (adapts) + Architecture (audit chain) |
| 5 | Email drafts with real safety | 2:46 | Innovation (mutates external systems) + Architecture (HITL, idempotent) |
| 6 | Voice + generative music | 3:04 | Bonus (Lyria) + Innovation (voice) |
| 7 | Under the hood — guardrails + failover | 3:30 | Architecture (30%) |
| 8 | Bonus models earned + close | 4:00 | Bonus (up to 0.6 pts) |

---

## The full voiceover (read this straight through)

### Beat 0 — Cold open (0:00 → 0:24)

*[SCREEN] Full-frame Google Calendar week view. Real chaos: a kid's soccer at 4pm, a "Mom PT?" event with a question mark, a co-parent's out-of-town block. Cursor drifts across it. Zoom on a Tuesday afternoon.*

**VO:**
> If you've ever run a family's calendar — a parent, a grown kid managing elder care, a partner holding a household together — you know the moment. A kid's soccer at four. Mom's PT at nine. And somewhere in your head, a small alarm goes off — *wait, are the shoes in the car?*
>
> Three in four family caregivers say they feel stressed. Level is what catches the shoes.

*[SUPER, bottom third]* `A Place for Mom · State of Caregiving 2025 · 75% of family caregivers feel stressed`

---

### Beat 1 — 10-second demo login (0:24 → 0:36)

*[SCREEN] Landing page → click **Try demo: Solo caregiver** → `/today` loads with agenda, people chips, sidebar all populated. Highlight the **Demo mode** pill in the top nav.*

**VO:**
> One click. No sign-up. Level seeds a caregiver — two kids, an elder mom — and two hundred pre-classified events. Everything from here runs live against Gemini.

*[SUPER]* `POST /v1/auth/demo · seeded from example-data/`

---

### Beat 2 — Proactive noticing on messy calendars (0:36 → 1:12)

*[SCREEN] Scroll to **"Level noticed while you slept"**. Zoom card: *"Nova's ballet is missing this week."* Hover three past ballet events on the agenda: `Nova ballet`, `Ballet - Nova`, `Nova ballet class`. Same event, three different names.*

**VO:**
> Overnight — without anyone asking — Level noticed something. Nova has ballet every Thursday. But not this Thursday. Nobody canceled it. It just isn't there.
>
> And here's what I love — the ballet isn't a recurring event on Google Calendar. Three past weeks, three different names: *Nova ballet*, *Ballet - Nova*, *Nova ballet class*. Real families don't tidy their calendars. Level clustered them anyway — same person, same weekday, same hour band — and majority-voted the name. All from a nightly Cloud Run Job while you slept.

*[SUPER]* `nightly Cloud Run Job · usuals clustered from messy events`

---

### Beat 3 — Priority-aware, sub-second booking (1:12 → 1:48)

*[SCREEN] Focus chat. Type *"prioritize elder care over sports this week"* — sidebar's **Priorities** section updates in under a second. Type *"when's the best time to book team dinner this week?"* — three slots appear, all inside 5–8pm. If one slot brushes an evening Helen PT block, the reply reads *"This would conflict with these priorities: elder care with Helen"* with *elder care with Helen* highlighted teal.*

**VO:**
> Watch this — I set a priority, and it lands in under a second. Level didn't call the LLM for that. A regex fast-path caught it. About sixty percent of every chat turn in this app never touches Gemini — that's what makes it feel instant.
>
> Now booking. Level knows dinner isn't seven a.m. It respects the priority I set twenty seconds ago. And when a slot conflicts with elder care, it says so — in teal, so the eye lands on it.

*[SUPER]* `fast-path registry · priority-aware slot ranking · meal-window inference`

**[BACKUP LINE]** *(if booking is slow >6s)*: *"This one calls the LLM because the phrasing is novel — you'll see it in the trace waterfall in a minute."*

---

### Beat 4 — Feedback loop that actually learns (1:48 → 2:18)

*[SCREEN] Click **Not me** on a Level suggestion. Reply lands: *"Removed — I won't propose that again."* Widen the data sidebar — a new `memory_bank` entry appears tagged `avoid`. Cut to `/admin/traces` in second window — two adjacent rows linked by `parent_audit_id`.*

**VO:**
> Every AI-authored thing in Level carries three chips — **Keep**, **Adjust**, **Not me**. Click one, and Level writes a second audit row that points back at the original. You can hand-verify the chain.
>
> The rejection I just clicked is now a memory tagged *avoid*. The next generator agent will read it as an anti-example. Not a mockup — the real data flow.

*[SUPER]* `parent_audit_id chain · Memory Bank tag=avoid`

---

### Beat 5 — Email drafts with real safety (2:18 → 2:46)

*[SCREEN] Type: *"email Ms. Anna that Nova is sick tomorrow"*. Active *"Drafting email..."* indicator (not a silent spinner). Draft renders — full to/subject/body with school context. Edit one line. Click **Send**. Toast: *"Preview only — Gmail send is disabled in demo mode."***

**VO:**
> This draft comes from an EmailAgent — Pro-tier, three refinement turns available, only used one. Every send goes through a confirmation token: idempotent, time-limited, only released once Google confirms success. A dying instance mid-send won't lose the draft. Clicking twice won't send twice. And nothing goes out until *I* click.

*[SUPER]* `EmailAgent · confirmation_token TTL · human-in-the-loop`

---

### Beat 6 — Voice + generative music (2:46 → 3:04)

*[SCREEN] Click **Hear my day** in the chat header. Soft chime plays 2–3 seconds. TTS reads the day summary. Caption text below.*

**VO** *(start speaking the moment the chime fades):*
> That chime is Lyria — Google's music model — synthesized fresh from a Vertex call. The voice that follows is a Gemini 3.5 Pro summary, cached by fingerprint so the second click comes back in milliseconds.

*[SUPER]* `Lyria (Vertex Interactions API) · SummaryAgent · fingerprint cache`

**[BACKUP LINE]** *(if chime fails)*: *"Chime is a bonus — Level degrades gracefully when a Vertex model isn't enabled, and the summary still plays."*

---

### Beat 7 — Under the hood (3:04 → 3:30)

*[SCREEN] `/admin/traces` full-screen — waterfall of agents grouped by `trace_id`, parent→child edges rendered. Cut to `/v1/admin/agents` (JSON): 11 agents, each with `name`, `model`, `safety_class`, `cost_tier`, `tools`. Cut to `/v1/admin/intents` — fast-path handlers. Brief pan over `docs/architecture.png`.*

**VO:**
> Every model call in this app is signed with an HMAC identity token. Six guardrails run before any prompt hits Gemini — Model Armor, rate and cost gate, PII strip, injection fence, source-span echo-back, signed identity. A circuit breaker isolates a bad Google backend per user. And a three-tier model ladder — Gemini 3.5, then 2.5, then Gemma — keeps chat alive during quota outages.

*[SUPER]* `signed AgentIdentity · circuit breaker · Gemma failover`

---

### Beat 8 — Bonus models earned + close (3:30 → 4:00)

*[SCREEN] Fast cuts: Veo weekly recap plays for 3 seconds. Trace waterfall row shows `fallback_used="gemma-3-4b-it"`. ADKPlannerAgent row. Then full-frame close card:*

```
Level
github.com/liujosh433-droid/Level
Live demo — one click, no signup
Built with Gemini 3.5 · ADK · Cloud Run · Firestore
#AllThingsAgenticHackathon
```

**VO:**
> Four bonus models, earned by working — not sprinkled. **ADK** on the email hot path. **Gemma** as real quota failover. **Veo 3** for the weekly recap you're seeing right now. **Lyria** for the chime you heard. Every one shows up in the trace waterfall.
>
> Level. Repo's public. Live demo is one click. Thanks for watching.

---

## Sources (cited in the cold open — link these in the video description too)

The cold open only quotes one number now, but here are the caregiver-scoped studies the framing is based on. Every one applies to any household manager, not just mothers.

1. **A Place for Mom (2025).** *State of Caregiving Report.* National survey of 1,029 US family caregivers, Sept 2025. Cold-open quote: **75%** of family caregivers report feeling stressed. Related: **71%** feel overwhelmed, **63%** report burnout at least monthly, **80%** don't feel confident balancing caregiving with everything else. Average **22.8 hours/week** on caregiving.
   https://img.prod.aplaceformom.com/main/uploads/lh/2025-Report-StateofCaregiving-VF-2.pdf
2. **Psychology Today (April 2024).** *Reframing the Mental Load of Parenting.* Gender-neutral definition: *"the work of managing a household and family that no one sees — remembering appointments, planning family activities, arranging house maintenance, booking travel, meals, school and camp registration."*
   https://www.psychologytoday.com/us/blog/parenting-translator/202404/reframing-the-mental-load-of-parenting
3. **AAPA + Harris Poll (May 2023).** US adults spend the equivalent of a full 8-hour workday every month coordinating healthcare. **65%** call it "overwhelming and time-consuming."
   https://www.globenewswire.com/news-release/2023/05/17/2670941/0/en/U-S-Adults-Spend-Eight-Hours-Monthly-Coordinating-Healthcare-Find-System-Overwhelming.html
4. **AARP / National Alliance for Caregiving (2025).** *Caregiving in the US 2025.* Roughly **1 in 4** US adults is a family caregiver; **64%** experience high emotional stress.
   https://doi.org/10.26419/ppi.00373.003

Every number said in the recording is above. Do not paraphrase them into a stronger claim.

---

## Recovery lines (memorize)

| If this fails mid-take | Say this |
|---|---|
| Chat reply is >8s slow | *"This one calls the LLM — you'll see it in the trace waterfall shortly."* |
| Lyria chime doesn't play | *"Chime is a bonus — Level degrades gracefully when a Vertex model isn't enabled, and the summary still plays."* |
| Veo endpoint 404 | *"We ship recorded footage for the recap in case a Vertex model is warming up — it's cached per week."* Cut to B-roll. |
| Traces page shows an old trace | *"That's the trace from the earlier click — the timestamps prove Level really is running this live."* |
| Demo button 404s (cloud demo off) | Cut to local demo B-roll immediately, no on-camera panic. |
| Feedback chip doesn't visibly re-influence | *"Adaptation shows on the next generator turn — one more click and you'd see the memory in the recall block."* |

---

## B-roll shot list (pre-record once, keep as fallback footage)

- Landing page → demo-button click → `/today` populated. **10 seconds.**
- Chat: *"prioritize elder care over sports"* + reply + sidebar update. **6 seconds.**
- Chat: *"when's the best time to book team dinner this week?"* + slot list. **8 seconds.**
- Feedback chip **Not me** click + `memory_bank` sidebar update. **5 seconds.**
- Email draft render + one edit + Send click + demo-mode toast. **12 seconds.**
- Hear my day: chime + TTS. **6 seconds.**
- `/admin/traces` waterfall with parent→child edges. **8 seconds.**
- `/v1/admin/agents` JSON scroll. **4 seconds.**
- Veo weekly recap (full 8-second clip in one take). **8 seconds.**
- Architecture diagram slow pan across the six planes. **8 seconds.**

Keep these in a folder called `demo-broll/` on the recording machine. Every one is a safety net.

---

## Voiceover pacing notes

- Target **150 wpm**. The spoken VO above totals ~540 words = 3:35, leaving 25 seconds for the app to react on-screen.
- **Land on the italicized `—` in the cold open.** The pause is what makes the "shoes in the car" line hit.
- Read the source name in the super flat — like a footnote, not a boast.
- **Never read a URL or file path out loud.** They live in supers.
- Say the word "Level" no more than five times total. Overuse feels like an ad.
- Punch the *verbs* — "noticed", "clustered", "caught", "flagged". They carry the innovation story more than the nouns do.
- End sentences on downbeats. Makes it sound like a product, not a pitch.

---

## What we deliberately don't show

Cut on purpose, not by accident. If a judge asks in the follow-up:

- **Circuit breaker** — mentioned in Beat 7 VO; live at `/v1/admin/calendar_circuit`.
- **Model Armor** — mentioned in Beat 7 VO; unit-tested in `tests/security/`.
- **PII scrub** — mentioned in Beat 7 VO; unit-tested.
- **Rate limit** — mentioned in Beat 7 VO; state at `/v1/admin/rate_limit`.
- **Nightly Cloud Run Job** — mentioned in Beat 2 VO; source in `packages/jobs/`.
- **Signed sessions + Firestore rules** — implicit in Beat 1's demo-login pill.

Every one of these is in the repo, in `SUBMISSION.md`, or in `docs/writeup-medium.md`. The demo's job is to make the judge *want* to open those links.
