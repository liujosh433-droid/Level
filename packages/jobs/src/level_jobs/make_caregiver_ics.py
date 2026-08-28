"""Generate realistic caregiver .ics fixtures for the Level demo.

Two scenarios are emitted into ``example-data/``:

* ``example-data/caregiver-month.ics`` - Family with a co-parent. Cast
  is Josh (the signed-in caregiver), Alex (co-parent), Nova + Theo
  (kids), and Helen (elder care). Alex shows up in recurring events
  so RoleAgent has enough weekly evidence to propose them as
  ``coparent``.

* ``example-data/caregiver-month-solo.ics`` - Same kids and same
  elder-care parent, but no co-parent anywhere. Josh does every
  pickup, dinner, and Sunday grocery drop alone. Slightly heavier
  Helen cadence, plus a standing biweekly sitter block, to match how
  a solo caregiver actually spends the week.

Both files cover Mon 2026-08-03 through Sun 2026-11-08 so Level can
infer usuals from four full weeks of history the moment a judge
imports the calendar AND every judge running any day between the
demo launch and mid-October still lands on a rich, non-empty agenda
after the whole-week date shift (see ``ics_loader._compute_shift_days``).
The current demo week (Mon 2026-08-24 through Sun 2026-08-30) is
engineered so that **three normally-recurring events are missing on
distinct weekdays** (Thu, Fri, Sun) - the nightly proactive-cards
job picks them up and renders them on ``/today`` as ``"Level
noticed while you slept"`` nudges. Spreading the misses across late
weekdays means judges who test on Fri, Sat, or Sun still see at
least one nudge for the current week (the ``today_wd`` filter in
``missing_usuals_this_week`` drops any weekday that has already
passed).

Weekly rhythm is a deliberate mix: RRULE for the true recurring
anchors (school dropoff, work, commute) and **individual VEVENTs
with slight text/time variance** for a handful of events that a
real caregiver would just re-create each week instead of turning
into a repeating series. See ``_messy_weekly`` at the bottom of
this module.

Three "messy usuals" (Nova ballet, Helen PT, grocery run) are
laid down as ~10 separate VEVENTs each - not a single RRULE -
with:

- text variance ("Nova ballet" / "Ballet - Nova" / "Nova ballet
  class")
- time wobble within the same hour band (4:30 vs 4:45 PM)

This proves the usuals engine clusters on
``(person_id, weekday, hour_band, activity_type)`` and picks the
majority-vote display name - not on exact string match of the
event summary. The three "missing this week" stories still hold:
Nova ballet is absent this Thursday, grocery run is absent this
Friday, and Helen's Sunday grocery drop is absent this Sunday.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

Scenario = Literal["family", "solo"]

TZ = ZoneInfo("America/Los_Angeles")
TZID = "America/Los_Angeles"
PROD_ID = "-//Level//Caregiver Month//EN"

# ---------------------------------------------------------------------------
# Calendar window - four weeks of history before "today" (Wed 2026-08-26),
# four weeks of runway after. RRULE UNTIL is a UTC stamp per RFC 5545.
# ---------------------------------------------------------------------------

START = date(2026, 8, 3)  # Monday
# END drives every RRULE UNTIL stamp. Judges test between the launch
# and Oct 1; ``ics_loader`` shifts the fixture forward by whole ISO
# weeks so anchor-week events always land in the judge's current
# week. A late-window judge (say Oct 1) sees roughly 5 shifted weeks
# after their "today" — so END has to sit ~5 weeks past the anchor's
# tail to keep the +28-day forward window populated. Nov 8 covers
# through mid-October judges with a couple of weeks of slack.
END = date(2026, 11, 8)  # Sunday
TODAY = date(2026, 8, 26)  # Wednesday - anchors the "missing this week" story

# Weekday shape of the demo week: Thu = 8/27, Fri = 8/28, Sat = 8/29, Sun = 8/30.
# Anchor: START (Mon 8/3) + 3 weeks = the demo week (Mon 8/24 - Sun 8/30).
# So the Nth _messy_weekly variant hits weekday-of-N-th-week; N=3 lands
# on the demo week. That's the "missing this week" slot for both ballet
# and grocery (encoded as ``skip_offsets=(THIS_WEEK,)``).
THIS_WEEK = 3

# Missing-usuals story for the current demo week: Nova ballet
# (Thu), the grocery run (Fri), and Helen's weekly Sunday grocery
# drop are all absent this week for both scenarios. The first two
# are ``_messy_weekly`` blocks with ``skip_offsets=(THIS_WEEK,)``;
# the third is a clean RRULE with an EXDATE for the anchor Sunday.
# Three misses spread across Thu/Fri/Sun guarantees at least one
# actionable nudge for judges who visit on any weekday - the
# ``missing_usuals_this_week`` filter drops any weekday earlier
# than today (you can't put back yesterday's ballet).
MISSING_HELEN_SUNDAY = date(2026, 8, 30)

LABOR_DAY = date(2026, 9, 7)
EARLY_PICKUP_DAYS = (date(2026, 8, 21), date(2026, 9, 10))
OFFSITE = date(2026, 9, 15)


def _local(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)


def _ics_local(d: date, hh: int, mm: int) -> str:
    return _local(d, hh, mm).strftime("%Y%m%dT%H%M%S")


def _until_utc() -> str:
    """UNTIL must be UTC when DTSTART carries a TZID (RFC 5545 §3.3.10)."""
    return _local(END, 23, 59).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _stamp() -> str:
    # DTSTAMP must be UTC per RFC 5545 §3.8.7.2; the trailing 'Z' is
    # the UTC sentinel. Previously this formatted local time but
    # appended 'Z', mislabelling every fixture as UTC. Now we
    # convert to UTC before formatting.
    return datetime.now(TZ).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _fold(line: str) -> str:
    if len(line) <= 75:
        return line
    parts = [line[:75]]
    rest = line[75:]
    while rest:
        parts.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(parts)


def _join(lines: list[str]) -> str:
    return "\r\n".join(_fold(x) for x in lines)


# ---------------------------------------------------------------------------
# VEVENT builders
# ---------------------------------------------------------------------------


def series(
    *,
    uid: str,
    first: date,
    start: tuple[int, int],
    end: tuple[int, int],
    summary: str,
    byday: str,
    location: str | None = None,
    interval: int = 1,
    exdates: list[date] | None = None,
) -> str:
    sh, sm = start
    eh, em = end
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_stamp()}",
        f"DTSTART;TZID={TZID}:{_ics_local(first, sh, sm)}",
        f"DTEND;TZID={TZID}:{_ics_local(first, eh, em)}",
        f"RRULE:FREQ=WEEKLY;BYDAY={byday};INTERVAL={interval};UNTIL={_until_utc()}",
        f"SUMMARY:{summary}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    if exdates:
        stamps = ",".join(_ics_local(d, sh, sm) for d in exdates)
        lines.append(f"EXDATE;TZID={TZID}:{stamps}")
    lines.append("END:VEVENT")
    return _join(lines)


def once(
    *,
    uid: str,
    day: date,
    start: tuple[int, int],
    end: tuple[int, int],
    summary: str,
    location: str | None = None,
) -> str:
    sh, sm = start
    eh, em = end
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_stamp()}",
        f"DTSTART;TZID={TZID}:{_ics_local(day, sh, sm)}",
        f"DTEND;TZID={TZID}:{_ics_local(day, eh, em)}",
        f"SUMMARY:{summary}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    lines.append("END:VEVENT")
    return _join(lines)


def _messy_weekly(
    *,
    uid_prefix: str,
    weekday_of_first: date,
    variants: list[tuple[str, tuple[int, int], tuple[int, int]]],
    location: str | None = None,
    skip_offsets: tuple[int, ...] = (),
) -> list[str]:
    """Emit N one-off VEVENTs mimicking a recurring event a caregiver
    never bothered to configure as recurring.

    Each variant is ``(summary_text, (start_h, start_m), (end_h, end_m))``
    and is applied to a successive week (0, 1, 2, ...) starting from
    ``weekday_of_first``. ``skip_offsets`` lets us omit particular weeks
    (used to keep the "missing this week" story intact - the demo relies
    on Nova ballet and grocery run being absent this specific week).

    The three-way variance (summary text, start hour+minute, end
    hour+minute) is deliberate: it exercises the usuals clustering on
    the (person_id, weekday, hour_band, activity_type) key while
    proving the display-name majority vote and hour_band bucketing
    both work on real-world-shaped data.

    UIDs are per-occurrence and unique so a downstream Google Calendar
    import doesn't collapse them into a series retroactively.
    """
    events: list[str] = []
    for i, (summary, start, end) in enumerate(variants):
        if i in skip_offsets:
            continue
        day = date.fromordinal(weekday_of_first.toordinal() + 7 * i)
        events.append(
            once(
                uid=f"{uid_prefix}-w{i}@level.local",
                day=day,
                start=start,
                end=end,
                summary=summary,
                location=location,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------


def _weekday_anchors() -> dict[str, date]:
    """First occurrence of each weekday inside the demo window."""
    return {
        "MO": date(2026, 8, 3),
        "TU": date(2026, 8, 4),
        "WE": date(2026, 8, 5),
        "TH": date(2026, 8, 6),
        "FR": date(2026, 8, 7),
        "SA": date(2026, 8, 8),
        "SU": date(2026, 8, 9),
    }


def build_family_events() -> list[str]:
    """Josh + Alex (co-parent) + Nova + Theo + Helen."""
    first = _weekday_anchors()
    weekday_holidays = [LABOR_DAY]
    pickup_holidays = [LABOR_DAY, *EARLY_PICKUP_DAYS]
    wfh_holidays = [OFFSITE]

    events: list[str] = [
        # -- Weekly usuals ---------------------------------------------------
        series(
            uid="fam-dropoff@level.local",
            first=first["MO"],
            start=(7, 45),
            end=(8, 15),
            summary="Nova + Theo dropoff",
            byday="MO,TU,WE,TH,FR",
            location="Riverside Elementary",
            exdates=weekday_holidays,
        ),
        series(
            uid="fam-pickup-nova@level.local",
            first=first["MO"],
            start=(15, 0),
            end=(15, 25),
            summary="Nova pickup",
            byday="MO,TU,WE,TH,FR",
            location="Riverside Elementary",
            exdates=pickup_holidays,
        ),
        # Co-parent Alex handles Theo's preschool pickup MO/WE/FR.
        series(
            uid="fam-theo-pickup-alex@level.local",
            first=first["MO"],
            start=(17, 15),
            end=(17, 45),
            summary="Theo pickup (Alex)",
            byday="MO,WE,FR",
            location="Little Sprouts Preschool",
            exdates=weekday_holidays,
        ),
        # Josh handles Theo pickup on the other two weekdays.
        series(
            uid="fam-theo-pickup-josh@level.local",
            first=first["TU"],
            start=(17, 15),
            end=(17, 45),
            summary="Theo pickup",
            byday="TU,TH",
            location="Little Sprouts Preschool",
        ),
        series(
            uid="fam-commute-in@level.local",
            first=first["MO"],
            start=(8, 20),
            end=(8, 50),
            summary="Commute to office",
            byday="MO,WE,FR",
            exdates=weekday_holidays,
        ),
        series(
            uid="fam-commute-out@level.local",
            first=first["MO"],
            start=(17, 0),
            end=(17, 30),
            summary="Commute home",
            byday="MO,WE,FR",
            exdates=weekday_holidays,
        ),
        series(
            uid="fam-work-office@level.local",
            first=first["MO"],
            start=(9, 0),
            end=(16, 45),
            summary="Work",
            byday="MO,WE,FR",
            location="Office",
            exdates=weekday_holidays,
        ),
        series(
            uid="fam-work-home@level.local",
            first=first["TU"],
            start=(9, 0),
            end=(16, 45),
            summary="Work",
            byday="TU,TH",
            location="Home",
            exdates=wfh_holidays,
        ),
        series(
            uid="fam-work-1on1@level.local",
            first=first["MO"],
            start=(9, 30),
            end=(10, 15),
            summary="Work 1:1",
            byday="MO",
            location="Office",
            exdates=weekday_holidays,
        ),
        series(
            uid="fam-lunch@level.local",
            first=first["MO"],
            start=(12, 0),
            end=(12, 30),
            summary="Lunch",
            byday="MO,TU,WE,TH,FR",
            exdates=weekday_holidays,
        ),
        # -- Kid activities --------------------------------------------------
        series(
            uid="fam-theo-soccer@level.local",
            first=first["SA"],
            start=(10, 0),
            end=(11, 15),
            summary="Theo soccer practice",
            byday="SA",
            location="Harbor Field",
        ),
        # Nova ballet is a "messy weekly": individual VEVENTs with text
        # variance ("Nova ballet" / "Ballet - Nova" / "Nova ballet
        # class"), no RRULE. Level still infers Thursday-afternoon
        # ballet as Nova's usual because clustering keys on
        # (person, weekday, hour_band, activity_type). Missing THIS
        # Thursday (2026-08-27) is the first of the three demo missing-
        # usuals stories - the nightly proactive-cards job surfaces it.
        #
        # Majority vote: most weeks are the clean "Nova ballet" form so
        # the picked display name settles on it. Weeks 0/1/2 drive the
        # historical majority; weeks 4-9 keep the ballet visible after
        # the whole-week shift for judges running later in the demo
        # window.
        *_messy_weekly(
            uid_prefix="fam-nova-ballet",
            weekday_of_first=first["TH"],
            variants=[
                ("Nova ballet", (16, 30), (17, 30)),        # week 0 - past, majority
                ("Ballet - Nova", (16, 45), (17, 45)),      # week 1 - past, variant
                ("Nova ballet", (16, 30), (17, 30)),        # week 2 - past, majority
                ("Nova ballet class", (16, 30), (17, 30)),  # week 3 (SKIPPED - missing)
                ("Nova ballet", (16, 30), (17, 30)),        # week 4
                ("Nova ballet", (16, 30), (17, 30)),        # week 5
                ("Nova ballet", (16, 30), (17, 30)),        # week 6
                ("Ballet - Nova", (16, 45), (17, 45)),      # week 7 - variant
                ("Nova ballet", (16, 30), (17, 30)),        # week 8
                ("Nova ballet", (16, 30), (17, 30)),        # week 9
            ],
            location="Studio B",
            skip_offsets=(THIS_WEEK,),
        ),
        # -- Elder care (Helen) ---------------------------------------------
        # Helen PT is a "messy weekly": individual VEVENTs with slight
        # text + time variance. Demo point: Level clusters these as a
        # Wednesday-morning usual for Helen even though Josh never
        # marked the event as recurring in Google Calendar and used
        # different wording each week.
        *_messy_weekly(
            uid_prefix="fam-helen-pt",
            weekday_of_first=first["WE"],
            variants=[
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 0
                ("Helen PT", (9, 45), (10, 45)),               # week 1
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 2 - majority text
                ("PT - Helen", (10, 15), (11, 15)),            # week 3 - this week, variant
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 4
                ("Helen PT", (9, 45), (10, 45)),               # week 5 - variant
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 6
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 7
                ("PT - Helen", (10, 15), (11, 15)),            # week 8 - variant
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 9
            ],
            location="Bayside PT Clinic",
        ),
        # Missing usual #3: Helen weekly grocery drop on Sun 2026-08-30.
        # Skips the anchor Sunday so late-week (Fri/Sat/Sun) judges still
        # see at least one nudge — Thursday (ballet) and Friday (grocery
        # run) have already passed by then. The RRULE still expands
        # every other Sunday across the demo window, so the visible
        # cadence stays intact.
        series(
            uid="fam-helen-grocery-drop@level.local",
            first=first["SU"],
            start=(10, 0),
            end=(11, 0),
            summary="Helen weekly grocery drop",
            byday="SU",
            location="Helen's apartment",
            exdates=[MISSING_HELEN_SUNDAY],
        ),
        series(
            uid="fam-call-helen@level.local",
            first=first["TU"],
            start=(19, 0),
            end=(19, 15),
            summary="Call Helen",
            byday="TU,TH",
        ),
        # -- Household -------------------------------------------------------
        # Grocery run is a "messy weekly": Josh sometimes types the
        # store name, sometimes just "Grocery run", never bothered to
        # make it recurring. Level's second missing-usuals story for
        # the demo week (absent this Friday 2026-08-28) - the nightly
        # proactive-cards job surfaces it alongside Nova ballet.
        # Majority-vote display picks "Grocery run".
        *_messy_weekly(
            uid_prefix="fam-grocery-run",
            weekday_of_first=first["FR"],
            variants=[
                ("Grocery run", (16, 15), (16, 50)),      # week 0 - past, majority
                ("Trader Joe's", (16, 30), (17, 5)),      # week 1 - past, variant
                ("Grocery run", (16, 15), (16, 50)),      # week 2 - past, majority
                ("Grocery pickup", (16, 45), (17, 20)),   # week 3 (SKIPPED - missing)
                ("Grocery run", (16, 15), (16, 50)),      # week 4
                ("Grocery run", (16, 15), (16, 50)),      # week 5
                ("Trader Joe's", (16, 30), (17, 5)),      # week 6 - variant
                ("Grocery run", (16, 15), (16, 50)),      # week 7
                ("Grocery run", (16, 15), (16, 50)),      # week 8
                ("Grocery run", (16, 15), (16, 50)),      # week 9
            ],
            skip_offsets=(THIS_WEEK,),
        ),
        series(
            uid="fam-family-dinner@level.local",
            first=first["FR"],
            start=(18, 30),
            end=(20, 0),
            summary="Family dinner with Alex",
            byday="FR",
            location="Home",
        ),
        series(
            uid="fam-library@level.local",
            first=first["SA"],
            start=(15, 0),
            end=(16, 0),
            summary="Nova + Theo library",
            byday="SA",
            location="Riverside Library",
        ),
        series(
            uid="fam-house-reset@level.local",
            first=first["SA"],
            start=(9, 0),
            end=(10, 0),
            summary="House reset / laundry",
            byday="SA",
            interval=2,
        ),
    ]

    # -- One-off events (past + upcoming) ------------------------------------
    one_offs: list[tuple[str, date, tuple[int, int], tuple[int, int], str, str | None]] = [
        # Past history - anchors context so RoleAgent has enough evidence.
        ("nova-pediatrician", date(2026, 8, 4), (10, 30), (11, 0), "Nova pediatrician", "Riverside Pediatrics"),
        ("helen-pharmacy", date(2026, 8, 7), (8, 30), (9, 0), "Helen pharmacy pickup", "Harbor Pharmacy"),
        ("nova-dentist", date(2026, 8, 12), (16, 0), (16, 40), "Nova dentist", "Smile Kids Dental"),
        ("plumber", date(2026, 8, 14), (10, 0), (11, 30), "Plumber - kitchen sink", "Home"),
        ("soccer-parents", date(2026, 8, 18), (18, 0), (19, 0), "Theo soccer parent meeting", "Harbor Field"),
        ("early-pickup", date(2026, 8, 21), (12, 30), (13, 0), "Nova early dismissal pickup", "Riverside Elementary"),
        ("theo-well", date(2026, 8, 24), (16, 15), (16, 50), "Theo well-check", "Riverside Pediatrics"),
        # Today.
        ("helen-followup", date(2026, 8, 26), (14, 0), (14, 40), "Helen follow-up", "Bayside PT Clinic"),
        # This week: explains why the co-parent isn't around for Friday dinner.
        ("alex-travel-1", date(2026, 8, 27), (8, 0), (17, 0), "Alex work travel - Detroit", None),
        ("alex-travel-2", date(2026, 8, 28), (8, 0), (17, 0), "Alex work travel - Detroit", None),
        # Future.
        ("back-to-school", date(2026, 9, 1), (17, 30), (18, 15), "Nova back-to-school night", "Riverside Elementary"),
        ("helen-cardio", date(2026, 9, 3), (13, 0), (14, 0), "Helen cardiology follow-up", "Pacific Heart"),
        ("labor-day-park", LABOR_DAY, (12, 0), (14, 0), "Labor Day park - Alex, Nova, Theo, Helen", None),
        ("nova-dentist-2", date(2026, 9, 8), (15, 40), (16, 20), "Nova dentist follow-up", "Smile Kids Dental"),
        ("class-party", date(2026, 9, 10), (14, 0), (15, 15), "Nova class party (early pickup)", "Riverside Elementary"),
        ("my-dentist", date(2026, 9, 11), (8, 30), (9, 0), "My dentist", "Downtown Dental"),
        ("offsite", OFFSITE, (12, 0), (16, 0), "Work offsite", "Downtown office"),
        ("helen-meds", date(2026, 9, 16), (17, 30), (18, 15), "Helen grocery + meds", "Harbor Pharmacy"),
        ("curriculum", date(2026, 9, 17), (18, 0), (19, 30), "Curriculum night - Theo's class", "Little Sprouts Preschool"),
        ("alex-birthday", date(2026, 9, 21), (19, 0), (21, 0), "Alex's birthday dinner", "Home"),
    ]
    for slug, day, start, end, summary, loc in one_offs:
        events.append(
            once(
                uid=f"fam-{slug}@level.local",
                day=day,
                start=start,
                end=end,
                summary=summary,
                location=loc,
            )
        )
    return events


def build_solo_events() -> list[str]:
    """Josh (solo) + Nova + Theo + Helen. No co-parent anywhere."""
    first = _weekday_anchors()
    weekday_holidays = [LABOR_DAY]
    pickup_holidays = [LABOR_DAY, *EARLY_PICKUP_DAYS]

    events: list[str] = [
        # -- Weekly usuals ---------------------------------------------------
        series(
            uid="solo-dropoff@level.local",
            first=first["MO"],
            start=(7, 45),
            end=(8, 15),
            summary="Nova + Theo dropoff",
            byday="MO,TU,WE,TH,FR",
            location="Riverside Elementary",
            exdates=weekday_holidays,
        ),
        series(
            uid="solo-pickup-nova@level.local",
            first=first["MO"],
            start=(15, 0),
            end=(15, 25),
            summary="Nova pickup",
            byday="MO,TU,WE,TH,FR",
            location="Riverside Elementary",
            exdates=pickup_holidays,
        ),
        # Solo parent: Josh handles Theo pickup every weekday.
        series(
            uid="solo-theo-pickup@level.local",
            first=first["MO"],
            start=(17, 15),
            end=(17, 45),
            summary="Theo pickup",
            byday="MO,TU,WE,TH,FR",
            location="Little Sprouts Preschool",
            exdates=weekday_holidays,
        ),
        # Only two office days - flexibility matters more when you're solo.
        series(
            uid="solo-commute-in@level.local",
            first=first["MO"],
            start=(8, 20),
            end=(8, 50),
            summary="Commute to office",
            byday="MO,WE",
            exdates=weekday_holidays,
        ),
        series(
            uid="solo-commute-out@level.local",
            first=first["MO"],
            start=(17, 0),
            end=(17, 30),
            summary="Commute home",
            byday="MO,WE",
            exdates=weekday_holidays,
        ),
        series(
            uid="solo-work-office@level.local",
            first=first["MO"],
            start=(9, 0),
            end=(16, 45),
            summary="Work",
            byday="MO,WE",
            location="Office",
            exdates=weekday_holidays,
        ),
        series(
            uid="solo-work-home@level.local",
            first=first["TU"],
            start=(9, 0),
            end=(16, 45),
            summary="Work",
            byday="TU,TH,FR",
            location="Home",
        ),
        series(
            uid="solo-lunch@level.local",
            first=first["MO"],
            start=(12, 0),
            end=(12, 30),
            summary="Lunch",
            byday="MO,TU,WE,TH,FR",
            exdates=weekday_holidays,
        ),
        # -- Kid activities --------------------------------------------------
        series(
            uid="solo-theo-soccer@level.local",
            first=first["SA"],
            start=(10, 0),
            end=(11, 15),
            summary="Theo soccer practice",
            byday="SA",
            location="Harbor Field",
        ),
        # Nova ballet - messy weekly. Same demo hook as the family
        # scenario (see the equivalent block in build_family_events).
        *_messy_weekly(
            uid_prefix="solo-nova-ballet",
            weekday_of_first=first["TH"],
            variants=[
                ("Nova ballet", (16, 30), (17, 30)),        # week 0 - past, majority
                ("Ballet - Nova", (16, 45), (17, 45)),      # week 1 - past, variant
                ("Nova ballet", (16, 30), (17, 30)),        # week 2 - past, majority
                ("Nova ballet class", (16, 30), (17, 30)),  # week 3 (SKIPPED - missing)
                ("Nova ballet", (16, 30), (17, 30)),        # week 4
                ("Nova ballet", (16, 30), (17, 30)),        # week 5
                ("Nova ballet", (16, 30), (17, 30)),        # week 6
                ("Ballet - Nova", (16, 45), (17, 45)),      # week 7 - variant
                ("Nova ballet", (16, 30), (17, 30)),        # week 8
                ("Nova ballet", (16, 30), (17, 30)),        # week 9
            ],
            location="Studio B",
            skip_offsets=(THIS_WEEK,),
        ),
        # -- Elder care (Helen) - heavier cadence for the solo caregiver -----
        # Helen PT - messy weekly, present this week in agenda.
        *_messy_weekly(
            uid_prefix="solo-helen-pt",
            weekday_of_first=first["WE"],
            variants=[
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 0
                ("Helen PT", (9, 45), (10, 45)),               # week 1
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 2 - majority
                ("PT - Helen", (10, 15), (11, 15)),            # week 3 - this week, variant
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 4
                ("Helen PT", (9, 45), (10, 45)),               # week 5 - variant
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 6
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 7
                ("PT - Helen", (10, 15), (11, 15)),            # week 8 - variant
                ("Helen physical therapy", (10, 0), (11, 0)),  # week 9
            ],
            location="Bayside PT Clinic",
        ),
        # Missing usual #2: Helen weekly grocery drop on Sun 2026-08-30.
        series(
            uid="solo-helen-grocery-drop@level.local",
            first=first["SU"],
            start=(10, 0),
            end=(11, 0),
            summary="Helen weekly grocery drop",
            byday="SU",
            location="Helen's apartment",
            exdates=[MISSING_HELEN_SUNDAY],
        ),
        series(
            uid="solo-helen-check-in@level.local",
            first=first["MO"],
            start=(20, 0),
            end=(20, 15),
            summary="Call Helen",
            byday="MO,WE,FR",
        ),
        # -- Household -------------------------------------------------------
        # Grocery run - messy weekly. Skips the anchor Friday so the
        # solo scenario shows the same three-day miss pattern as
        # family (Thu ballet + Fri grocery + Sun Helen drop). A judge
        # running on Fri/Sat has ballet already past; the Friday miss
        # and the Sunday miss keep at least one nudge on screen.
        *_messy_weekly(
            uid_prefix="solo-grocery-run",
            weekday_of_first=first["FR"],
            variants=[
                ("Grocery run", (16, 15), (16, 50)),      # week 0 - past, majority
                ("Trader Joe's", (16, 30), (17, 5)),      # week 1 - past, variant
                ("Grocery run", (16, 15), (16, 50)),      # week 2 - past, majority
                ("Grocery pickup", (16, 45), (17, 20)),   # week 3 (SKIPPED - missing)
                ("Grocery run", (16, 15), (16, 50)),      # week 4
                ("Grocery run", (16, 15), (16, 50)),      # week 5
                ("Trader Joe's", (16, 30), (17, 5)),      # week 6 - variant
                ("Grocery run", (16, 15), (16, 50)),      # week 7
                ("Grocery run", (16, 15), (16, 50)),      # week 8
                ("Grocery run", (16, 15), (16, 50)),      # week 9
            ],
            skip_offsets=(THIS_WEEK,),
        ),
        series(
            uid="solo-meal-prep@level.local",
            first=first["SU"],
            start=(16, 0),
            end=(17, 0),
            summary="Sunday meal prep",
            byday="SU",
            location="Home",
        ),
        series(
            uid="solo-library@level.local",
            first=first["SA"],
            start=(15, 0),
            end=(16, 0),
            summary="Nova + Theo library",
            byday="SA",
            location="Riverside Library",
        ),
        series(
            uid="solo-house-reset@level.local",
            first=first["SA"],
            start=(9, 0),
            end=(10, 0),
            summary="House reset / laundry",
            byday="SA",
            interval=2,
        ),
        # Biweekly sitter block - the only "me time" a solo caregiver books.
        series(
            uid="solo-sitter-night@level.local",
            first=first["SA"],
            start=(19, 0),
            end=(21, 0),
            summary="Sitter - my night out",
            byday="SA",
            interval=2,
        ),
    ]

    one_offs: list[tuple[str, date, tuple[int, int], tuple[int, int], str, str | None]] = [
        ("nova-pediatrician", date(2026, 8, 4), (10, 30), (11, 0), "Nova pediatrician", "Riverside Pediatrics"),
        ("helen-pharmacy", date(2026, 8, 7), (8, 30), (9, 0), "Helen pharmacy pickup", "Harbor Pharmacy"),
        ("nova-dentist", date(2026, 8, 12), (16, 0), (16, 40), "Nova dentist", "Smile Kids Dental"),
        ("plumber", date(2026, 8, 14), (10, 0), (11, 30), "Plumber - kitchen sink", "Home"),
        ("soccer-parents", date(2026, 8, 18), (18, 0), (19, 0), "Theo soccer parent meeting", "Harbor Field"),
        ("early-pickup", date(2026, 8, 21), (12, 30), (13, 0), "Nova early dismissal pickup", "Riverside Elementary"),
        ("theo-well", date(2026, 8, 24), (16, 15), (16, 50), "Theo well-check", "Riverside Pediatrics"),
        ("helen-followup", date(2026, 8, 26), (14, 0), (14, 40), "Helen follow-up", "Bayside PT Clinic"),
        # One-off sitter this Saturday since Josh needs a break the demo week.
        ("sitter-relief", date(2026, 8, 29), (18, 0), (21, 0), "Sitter - evening relief", "Home"),
        ("back-to-school", date(2026, 9, 1), (17, 30), (18, 15), "Nova back-to-school night", "Riverside Elementary"),
        ("helen-cardio", date(2026, 9, 3), (13, 0), (14, 0), "Helen cardiology follow-up", "Pacific Heart"),
        ("labor-day-park", LABOR_DAY, (12, 0), (14, 0), "Labor Day park with Nova and Theo", None),
        ("nova-dentist-2", date(2026, 9, 8), (15, 40), (16, 20), "Nova dentist follow-up", "Smile Kids Dental"),
        ("class-party", date(2026, 9, 10), (14, 0), (15, 15), "Nova class party (early pickup)", "Riverside Elementary"),
        ("my-dentist", date(2026, 9, 11), (8, 30), (9, 0), "My dentist", "Downtown Dental"),
        ("helen-annual", date(2026, 9, 15), (10, 0), (11, 30), "Helen annual physical", "Pacific Heart"),
        ("helen-meds", date(2026, 9, 16), (17, 30), (18, 15), "Helen grocery + meds", "Harbor Pharmacy"),
        ("curriculum", date(2026, 9, 17), (18, 0), (19, 30), "Curriculum night - Theo's class", "Little Sprouts Preschool"),
    ]
    for slug, day, start, end, summary, loc in one_offs:
        events.append(
            once(
                uid=f"solo-{slug}@level.local",
                day=day,
                start=start,
                end=end,
                summary=summary,
                location=loc,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Assembly + CLI
# ---------------------------------------------------------------------------

_SCENARIO_META: dict[Scenario, tuple[str, str, str]] = {
    # scenario -> (filename, X-WR-CALNAME, event-builder key)
    "family": (
        "caregiver-month.ics",
        "Level demo - Josh, Alex, Nova, Theo, Helen (family)",
        "family",
    ),
    "solo": (
        "caregiver-month-solo.ics",
        "Level demo - Josh, Nova, Theo, Helen (solo caregiver)",
        "solo",
    ),
}


def _build_events(scenario: Scenario) -> list[str]:
    if scenario == "family":
        return build_family_events()
    return build_solo_events()


def _assemble(scenario: Scenario) -> str:
    _, cal_name, _ = _SCENARIO_META[scenario]
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PROD_ID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{cal_name}",
        f"X-WR-TIMEZONE:{TZID}",
        "BEGIN:VTIMEZONE",
        f"TZID:{TZID}",
        "X-LIC-LOCATION:America/Los_Angeles",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:-0800",
        "TZOFFSETTO:-0700",
        "TZNAME:PDT",
        "DTSTART:20260308T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:-0700",
        "TZOFFSETTO:-0800",
        "TZNAME:PST",
        "DTSTART:20261101T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
        *_build_events(scenario),
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(body)


def write_scenario(scenario: Scenario, docs_dir: Path) -> Path:
    filename, _, _ = _SCENARIO_META[scenario]
    out = docs_dir / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    body = _assemble(scenario)
    out.write_text(body, encoding="utf-8")
    # Mirror into the packaged copy that ships inside the level_core
    # wheel (loaded at runtime via importlib.resources). Two locations
    # by design: example-data/ is human-facing (docs, generator, git
    # blame), level_core/demo/data/ is what Cloud Run's site-packages
    # actually sees. tests/unit/test_demo_seeder.py guards against
    # drift, so keeping them in sync HERE means no contributor ever
    # has to remember the second copy.
    packaged = _packaged_dir() / filename
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_text(body, encoding="utf-8")
    return out


def _out_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "example-data"


def _packaged_dir() -> Path:
    """In-wheel copy of the ICS fixtures - see write_scenario."""
    return (
        Path(__file__).resolve().parents[4]
        / "packages"
        / "core"
        / "src"
        / "level_core"
        / "demo"
        / "data"
    )


def main() -> list[Path]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=["family", "solo", "both"],
        default="both",
        help="Which fixture to (re)generate. Defaults to both.",
    )
    args = parser.parse_args()

    out_dir = _out_dir()
    scenarios: list[Scenario] = (
        ["family", "solo"] if args.scenario == "both" else [args.scenario]  # type: ignore[list-item]
    )
    written: list[Path] = []
    for scenario in scenarios:
        path = write_scenario(scenario, out_dir)
        text = path.read_text()
        print(
            f"Wrote {path.name} ({path.stat().st_size} bytes, "
            f"{text.count('RRULE:')} series, "
            f"{text.count('BEGIN:VEVENT')} events)"
        )
        written.append(path)
    return written


if __name__ == "__main__":
    main()
