# Fixtures

## Sample busy-parent calendar

Persona: **Maya** — kid (Jordan), full-time job + night class, senior care for Mom.  
Dates: ~Aug 3–23, 2026 (Pacific).

Google Calendar **does not keep per-event colors** from a single `.ics` import. To get color coding, import the **four category files** as separate calendars, then set each calendar’s color.

| File | What’s in it | Suggested GCal color |
| --- | --- | --- |
| `sample_busy_parent_kid.ics` | School, soccer, dentist, co-parent weekends | **Blueberry** |
| `sample_busy_parent_work.ics` | Meetings, night class, deep work | **Basil** |
| `sample_busy_parent_senior.ics` | Mom’s appointments, pharmacy, check-ins | **Tomato** |
| `sample_busy_parent_life.ics` | Groceries, budget, therapy, meal prep | **Graphite** |

`sample_busy_parent.ics` is the all-in-one file (same events, with `CATEGORIES` tags) if you prefer a single calendar.

## Sample busy caregiver — Casey (alt persona)

File: **`sample_busy_caregiver_casey.ics`** (single import).

Persona: **Casey** — sandwich caregiver. Kids **Nova** (preschool) + **Theo** (elementary), aging dad **Robert / Papa** (dialysis + day program), co-parent **Morgan**, job at **Northwind Health** (care coordination / social work).

Dates: ~Aug 4 – Sep 4, 2026 (Pacific) — covers “today” around mid-Aug for fresh-account testing.

Titles are intentionally messy (abbreviations, vague “Meeting” / “Pickup”, ALL-CAPS holds, work vs personal lookalikes) so you can see how Care Profile + week load classify without the clean Maya/Jordan wording.

### Import (Casey)

1. Google Calendar → Settings → **Import & export**
2. Import `sample_busy_caregiver_casey.ics` → new calendar e.g. `Level — Casey sample`
3. New Level account → Connect Google → Bring in my week

Tip: Don’t import both Maya and Casey onto the same Google account if you want a clean A/B — use a fresh calendar or a second Google user.

### Import (Maya — color-coded)

1. Open [Google Calendar](https://calendar.google.com) → Settings → **Import & export**
2. Import `sample_busy_parent_kid.ics` → create/use a calendar named e.g. `Level — Kid`
3. Repeat for Work, Senior, Life
4. In the left sidebar, open each calendar’s menu → **Color** → pick the suggested color above
5. In Level: Connect Google → Bring in my week

Tip: Keep these on separate calendars so you can hide or delete the sample later without touching your real schedule.
