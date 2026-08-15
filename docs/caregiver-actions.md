# Level — caregiver actions (not a scheduler)

Actions that **write the bind into Google** — school, the hole in the day, the paper on the counter — so the world has to trip over it.

These are candidates to score **operational utility** without violating the product lock: Level does not find more hours, lean on the village, or Tetris the week. Co-parent / friend messaging is **not** the product (see [caregiver-research.md](caregiver-research.md)).

Every write sits behind **Hold / Run**. None of them invent a spare adult.

**Product law — no hardcoded people.** Names in this doc are **illustrations only**. Runtime infers zero-to-many `CarePerson`s from *this user’s* calendar and Keep / Not me.

**Product law — AI-first, same as Care Profile.** Live inference uses Gemini structured output (`care_infer_llm` / new usual wrappers). Regex and static title clustering stay behind `LEVEL_ALLOW_HEURISTIC_CARE` and tests only — we do not invent usuals, people, or gaps with patterns in production (`persist.py` already refuses regex invent for roles). Glue code may list events, merge Keep, write Calendar, and fill copy templates. It may not decide *what counts as pickup* or *who a title refers to*.

---

## 1. The missing usual — a sticky window that didn’t show up

**The pattern, not the double-book.**

Caregivers don’t only collide with a *new* yes. They also drop a sticky window by omission: this Thursday never got the usual pickup, clinic, or after-school slot — deleted, skipped, or never created. The hole is invisible until the time hits.

A household can have **many people and many usuals** (two kids, different pickup times; Friday clinic for a parent). Each usual is owned by one `CarePerson`. Gaps are detected **per usual**, so one child’s missing soccer does not fire the other’s pickup.

Level already infers sticky windows and Keep. This action is the **dog that didn’t bark**:

1. **Learn the usual (AI, infrequent).** Same path as Care Profile: agenda window + previous people/usuals → Gemini structured `UsualInfer`. The model names repeating obligations, who they belong to, weekday/time, and a label — messy titles included. Code does **not** regex-cluster. Keep / Not me **locks** the record so the next ingest cannot reinvent it.
2. **Notice the absence (not AI).** Once a usual is Keep’d, missing is calendar arithmetic: that weekday/window, that person, no matching instance, no exception. Re-asking Gemini “is pickup missing?” every job is how you get false nags.
3. **Tell them on Today.** Short, cited, from the record: “{usual.label} usually sits here. It isn’t on the calendar this week.”
4. **They resolve it (Run).**
   - **Put it back** — insert an event from the usual (same title/window/person).
   - **This week is different** — exception on **that usual + date**. Don’t nag that instance again.
   - **Not me** — the pattern is wrong for **that usual or that person**; stop treating it as usual.

**Also fire when a yes creates the hole.** They confirm an event that crowds a Keep’d usual. Level does not find coverage. It **inserts** an uncovered block titled from the usual (`Uncovered: {label}`) for that instance (or the absence detector fires because the usual was crowded out). The gap stays until they resolve it: I’ll take it / already handled / leave it.

Anyone looking at the week sees the hole. Mental load becomes a block, not a feeling.

**Demo (data, not code):** a fixture calendar with a repeating person-bound usual and one missing instance — whoever that person is in the fixture. Today names them from the Care Profile. Run → Put it back → event exists.

**Not this:** invent who covers it, text a friend, offer sitters, hardcode a child named in the repo.

---

## 2. School paper (the actual parent chore)

Photo or Gmail PDF of the permission slip / spirit week / vaccine form. Extract the **deadline and the send-to**, put a dated hold on Calendar, draft the reply or the “signed” email to the **school address on the form** (institution, not a friend). Household-logistics role.

**Demo:** upload the slip → Thursday 8am hold → Run sends the teacher email.

---

## 3. Cancel cascade

Practice cancelled, early dismissal, “no clinic Friday” in Gmail. **Patch that instance**, lift the Keep for that day only, one line on Today: “Thu pickup window is free — you didn’t get the hours back until now.” Last-minute school/clinic mail is the real async job.

**Demo:** fixture email → soccer vanishes → Today updates. No new plan offered.

---

## 4. Seal the Keep

Recurring **busy** on a Keep’d window so work invites show you as busy. Pair with **draft-decline** on an invite that lands on that window (they Run the decline; the host gets it).

**Demo:** Keep pickup → busy series exists → late standup invite → Run → declined.

---

## 5. This week’s Keeps, published

Not “ask for help.” A **Doc or a second calendar** they already share: only sticky windows, named by role. The load is visible to a co-parent *if they have one* — optional, never the fix.

**Demo:** generate “Week of Aug 17 — Keeps” in Drive; optional “add to Family calendar.”

---

## 6. Sick-day school note

They name a person Level already holds (“{child} is home sick”). Resolve against `people_profiles` (aliases, display_name). One institutional send: attendance / teacher from **that person’s** `SchoolAnchor`, plus cancel **today’s events tagged to that person**. If two kids are sick, two proposals (or one with two recipients) — never “the kid.” Not a coverage search.

**Demo:** they say it → Run → email in Sent, that person’s events cancelled. Person comes from profile, not a string in the intent parser.

---

## Film two

**#1 (missing usual / uncovered pickup)** is the Level-shaped one — the collision or the omission becomes an object.  
**#2 (school paper)** is the “it actually did something” beat judges feel.

Seal the Keep (#4) if you only have time for a Calendar API win.

---

## Skip

Text a friend, find a sitter, rebalance the week, self-care blocks, lunch-money bots, collecting a “village” roster at onboarding.

---

## Data: usuals live on each person, not on a role bucket

Today a person is a **string** on a role (`CareRoleState.people`) plus `CareProfile.person_relationships[name]`. Sticky time is a `ProtectedWindow` on **child_care** / **elder_care**, not on a human. Two children with two pickup times cannot adapt; an elder’s clinic gets mashed into the same bucket.

Promote **zero-to-many** first-class care people. The user is one caregiver; each dependent (or partner) owns their usuals and school contacts.

`your_role` / `their_relation` are inferred phrases (parent, adult child, co-parent, …), not an enum of demo characters. A sandwich caregiver can have two `CarePerson`s on `child_care` and one on `elder_care` at once.

```
CarePerson
  person_id             stable id (not the display name)
  display_name          from calendar / Keep — whatever they actually use
  aliases               nicknames, collapsed by infer (already CarePersonAssign)
  their_relation        who they are to the user (child, parent, …)
  your_role             how the user stands toward them (parent, adult child, …)
  care_role_id          child_care | elder_care | partner_coparent | …
  status                Keep | Not me | pending
  school                SchoolAnchor?  # optional; only if that person has school
      attendance_email
      teacher_email
      teacher_label
  usuals[]              UsualWindow    # 0–N per person
```

```
UsualWindow
  usual_id
  person_id             FK to that CarePerson
  label                 model-written, from the repeating obligation
  weekday               0–6 as inferred (not parsed from the title with regex)
  start_minute, end_minute
  hit_count / miss_count
  last_seen_on          date
  confidence            0–1  # model + Keep: hit ↑, miss+exception ↓, Not me → rejected
  evidence              short citation (event summaries the model used)
  exceptions[]          dates they marked "this week is different"
  status                active | rejected
```

Drop `title_fingerprint` as a clustering key. If we store a fingerprint at all, it is only a cache hint after the model has already assigned an event to a usual — never how we *discover* usuals.

Infer already emits `CarePersonAssign` (name, role, relationship). Persist a **list** of `CarePerson`, never a single hardcoded slot. Keep / Not me on the **person** and on **each usual**.

Scan **all** active usuals for gaps. Copy and banners loop; they do not special-case the first child.

---

## Feasibility (1, 2, 6)

| # | Effort | Why |
|---|---|---|
| **1 Missing usual** | **Medium — do first.** ~2–4 days | Calendar cache + `create_calendar_event` already exist. `async_challenge` is the same loop (scan agenda vs Care Profile → open a Decision). You are adding **absence** next to **overlap**. Need slightly more history than today’s 14-day lookback (`calendar_window` in `google_live.py`) — ~6 weeks back to mine a usual. |
| **2 School paper** | **Medium-hard.** ~3–5 days | No Gmail send today. OAuth is `calendar.events` only (`GOOGLE_SCOPES`). Need `gmail.send`, a PDF/photo upload (or a fixture), Gemini extract (deadline, to:), Calendar hold, Hold/Run. Demo can skip Gmail *ingest* and use upload. |
| **6 Sick-day note** | **Easy once #2’s send path exists.** ~1–2 days | Same `SchoolAnchor` + `gmail.send`. Day check-in already takes free text (`/v1/today` check-in). Resolve the named person against `people_profiles` (no name literals in the parser) → proposal: send that person’s attendance email + cancel *today’s* events tagged to them. Ambiguous (“the kids are sick”) → ask which people, from the list. Without #2, you’d still build send from scratch (then #2 is cheaper). |

**2 and 6 share the pipe:** person.school contacts, Gmail send, Hold/Run. Build that once. #1 does not need Gmail.

**Suggested order:** #1 → shared school contact + send → #6 (tiny UX) → #2 (extract UI). Film #1 and #6 if time is short; #2 is the flashier send-from-paper.

---

## Architecture into this repo

Existing loops (see [ARCHITECTURE.md](../ARCHITECTURE.md)):

- Ingest 15 min → Calendar cache (`CalendarSyncStore`) → Care Profile mutate (`persist_care_profile_from_events`)
- `async_challenge` → `find_role_collisions` (event **overlaps** a window) → open Decision
- Today → `pending_challenges` + events
- Confirm commitment → `create_calendar_event`

### Schema (`packages/core/src/level_core/schemas/care.py`)

- Add `CarePerson`, `UsualWindow`, `SchoolAnchor`.
- Add `CareProfile.people_profiles: list[CarePerson]` (keep `person_relationships` as a derived view for the graph so UI doesn’t break).
- Stop hanging new windows only on `CareRoleState.protected_windows`; **copy** usuals onto the person. Roles still get salience/load.

### Mine vs check — AI proposes, the clock checks

Same law as Care Profile (`persist.py` / `care_infer_llm.py`): **do not invent people or roles with regex.** Heuristic infer stays opt-in / tests only.

Usuals are different from roles in one way: a **gap** has to be a yes/no against the calendar. If Gemini re-clusters titles every 15 minutes, Thursday pickup becomes a new object each week and you either miss the hole or nag on noise.

| Step | Use AI? | Why |
|---|---|---|
| **Propose usuals** (who, what, which weekday/window) | **Yes — Gemini structured output**, same family as `infer_care_profile_ai`. Infrequent: when the agenda fingerprint changes, not on every poll. | Titles vary (“pickup”, “school run”, “get M”). People and nicknames are already AI. Regex cannot own this. |
| **Lock** | Human Keep / Not me on each usual | Stops the model from rewriting the series. |
| **Is this event an instance of a locked usual?** | **No model.** Time overlap with that usual’s window + event already tagged to that `person_id` (tags come from the existing AI event assign). Optional: Google `recurringEventId` when they actually used a series. | Stable, cheap, auditable. |
| **Is this usual missing this day?** | **No model.** Horizon dates × locked usuals minus matching instances minus `exceptions`. | The product is the empty slot, not another classification. |

Hit/miss counts are bookkeeping **after** those checks, not a clustering algorithm.

**Propose pass** (extend `CareHolisticInfer` or a sibling wrapper in `care_infer_llm.py`): send ~6–8 weeks of `{summary, start, end}` plus current `people_profiles`. Model returns usuals: `person` (must be one of the listed people or a new `CarePersonAssign`), `label`, weekday, start/end, evidence event ids. Merge like roles: never drop a Keep’d usual because the model forgot it; never add a usual the model invented if the user Not-me’d that fingerprint.

Widen `calendar_window(..., days_back=42)` so the propose pass has evidence. Forward window can stay ~4 weeks.

Do **not** add a parallel regex clusterer in `level_core.calendar.usuals`. A small module is fine for **gap arithmetic** only (`find_usual_gaps(locked_usuals, dated_events)`).

### Detect absence (jobs)

Extend **`packages/jobs/src/level_jobs/async_challenge.py`** (don’t add a third scheduler if you can avoid it):

1. Existing: `find_role_collisions` → overlap (unchanged).
2. New: `find_usual_gaps` → **deterministic** on Keep’d usuals: no matching instance that day, not in `exceptions`, status active.

Open a Decision with `origin="async_usual_gap"` (parallel to `async_role_theft`). Idempotent: skip if an OPEN decision already has that usual_id + date.

Conductor prompt is a **template** filled from the `UsualWindow` + `CarePerson` (label, weekday, person, your_role). No names in the prompt file. Challenger stays citation-shaped; **Put it back** is the action, not a lecture.

Optional: if they **confirm** a colliding event, also insert `Uncovered: {usual.label}` via `create_calendar_event` (same resolver).

### Today + Run (`packages/api/.../today.py`, `today/page.tsx`)

- Add `usual_gaps[]` on `TodayView` (label, person, relationship, when, usual_id).
- Banner next to care-collision, **one row per gap**: “{label} usually sits here for {display_name} (you’re the {your_role}).” Multiple kids/elders → multiple rows.
- Actions: Put it back | This week is different | Not me — same posture as Keep / Not me.
- Put it back → existing `create_calendar_event` (`ingest/google_live.py` + `routes/calendar.py`).
- Exception / Not me → patch `UsualWindow` on the person, bump Care Profile version.

### #2 + #6 (shared)

| Piece | Where |
|---|---|
| `SchoolAnchor` on `CarePerson` | schema + Profile UI (email fields, Keep) |
| Incremental OAuth `gmail.send` | `auth/google_oauth.py` `GOOGLE_SCOPES` |
| `send_gmail(...)` | `ingest/google_live.py` next to calendar insert |
| Hold/Run proposal | new kind on `CommitmentProposal` or `SchoolNoteProposal` in `schemas/commitment.py` |
| #6 intent | Gemini on the check-in text + `people_profiles` list → which `person_id`s (or ask). Then send that person’s school email + cancel today’s events for those ids. No name regex, no default child. |
| #2 extract | `POST /v1/school-paper` (upload) → Gemini 3.5 (PDF/image) → deadline, to:, which `CarePerson` (match name on the slip to profiles, or ask). Calendar hold + proposal. Demo PDF belongs in `fixtures/`, not in the extractor. |
| Cancel today’s events for a person | Calendar API `patch`/`delete` that instance only — match via person tag / title fingerprint, not “all child_care”. |

Model Armor on the slip (names, student ID) before Gemini. Human Run before send.

### Tests

- `test_usuals.py` — **constructed** agendas (two people, two usuals): A missing, B present → only A’s gap; exception suppresses; Not me on A does not kill B. Names are test locals, not imported from production.
- `test_role_collisions.py` stays overlap-only.
- Judge demo: calendar JSON under `fixtures/` loaded as user data. `make demo-judge` must not contain person names in Python/TS source.

### What not to do

- Don’t re-run Gemini every poll to “find patterns” — propose on agenda change, lock with Keep, **check gaps in code**.
- Don’t regex-invent people, pickup, or school from titles (same as Care Profile).
- Don’t hang usuals only on `child_care` / a single “the kid” — N people × N usuals.
- Don’t put display names, schools, or weekdays in product source. Templates + records only.
- Don’t use Gmail ingest for v1 of #2 (no `gmail.readonly` yet); upload + `fixtures/` is enough.
- Don’t auto-insert a usual without Run.
- Don’t text a co-parent from a gap.
