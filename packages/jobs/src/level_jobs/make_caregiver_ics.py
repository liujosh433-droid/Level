"""Generate a realistic busy-caregiver .ics for Google Calendar import.

Weekly rhythm is real RRULE series (so Google can delete "this and following
events"). One-offs stay single VEVENTs. Covers Mon 2026-08-03 through
Fri 2026-09-18 so Level can infer usuals from past weeks after you sync.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
TZID = "America/Los_Angeles"
PROD_ID = "-//Level//Caregiver Month//EN"
CAL_NAME = "Level caregiver demo (Nova, Theo, Helen)"

START = date(2026, 8, 3)  # Monday
END = date(2026, 9, 18)  # Friday
LABOR_DAY = date(2026, 9, 7)
EARLY_PICKUP = (date(2026, 8, 21), date(2026, 9, 10))
OFFSITE = date(2026, 9, 15)


def _local(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)


def _ics_local(d: date, hh: int, mm: int) -> str:
    return _local(d, hh, mm).strftime("%Y%m%dT%H%M%S")


def _until_utc() -> str:
    """Last moment of END in LA, as UTC — RRULE UNTIL must be UTC with TZID DTSTART."""
    return _local(END, 23, 59).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _stamp() -> str:
    return datetime.now(TZ).strftime("%Y%m%dT%H%M%SZ")


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


def build_events() -> list[str]:
    first = {
        "MO": date(2026, 8, 3),
        "TU": date(2026, 8, 4),
        "WE": date(2026, 8, 5),
        "TH": date(2026, 8, 6),
        "FR": date(2026, 8, 7),
        "SA": date(2026, 8, 8),
        "SU": date(2026, 8, 9),
    }
    weekday_ex = [LABOR_DAY]  # Monday series skip school/work
    pickup_ex = [LABOR_DAY, *EARLY_PICKUP]
    wfh_ex = [OFFSITE]  # Tue work block replaced by morning + offsite

    events = [
        series(
            uid="level-dropoff@level.local",
            first=first["MO"],
            start=(7, 45),
            end=(8, 15),
            summary="Nova + Theo dropoff",
            byday="MO,TU,WE,TH,FR",
            location="Riverside Elementary",
            exdates=weekday_ex,
        ),
        series(
            uid="level-pickup-nova@level.local",
            first=first["MO"],
            start=(15, 0),
            end=(15, 25),
            summary="Nova pickup",
            byday="MO,TU,WE,TH,FR",
            location="Riverside Elementary",
            exdates=pickup_ex,
        ),
        series(
            uid="level-commute-in@level.local",
            first=first["MO"],
            start=(8, 20),
            end=(8, 50),
            summary="Commute to office",
            byday="MO,WE,FR",
            exdates=weekday_ex,
        ),
        series(
            uid="level-commute-out@level.local",
            first=first["MO"],
            start=(17, 10),
            end=(17, 40),
            summary="Commute home",
            byday="MO,WE,FR",
            exdates=weekday_ex,
        ),
        series(
            uid="level-work-office@level.local",
            first=first["MO"],
            start=(9, 0),
            end=(14, 45),
            summary="Work",
            byday="MO,FR",
            location="Office",
            exdates=weekday_ex,
        ),
        series(
            uid="level-work-home@level.local",
            first=first["TU"],
            start=(9, 0),
            end=(14, 45),
            summary="Work",
            byday="TU,TH",
            location="Home",
            exdates=wfh_ex,
        ),
        series(
            uid="level-work-wed@level.local",
            first=first["WE"],
            start=(11, 15),
            end=(14, 45),
            summary="Work",
            byday="WE",
            location="Office",
        ),
        series(
            uid="level-work-1on1@level.local",
            first=first["MO"],
            start=(9, 30),
            end=(10, 15),
            summary="Work 1:1",
            byday="MO",
            location="Office",
            exdates=weekday_ex,
        ),
        series(
            uid="level-lunch@level.local",
            first=first["MO"],
            start=(12, 0),
            end=(12, 30),
            summary="Lunch",
            byday="MO,TU,WE,TH,FR",
            exdates=weekday_ex,
        ),
        series(
            uid="level-helen-pt@level.local",
            first=first["WE"],
            start=(10, 0),
            end=(11, 0),
            summary="Helen physical therapy",
            byday="WE",
            location="Bayside PT Clinic",
        ),
        series(
            uid="level-theo-soccer@level.local",
            first=first["TU"],
            start=(16, 0),
            end=(17, 15),
            summary="Theo soccer practice",
            byday="TU",
            location="Harbor Field",
        ),
        series(
            uid="level-theo-swim@level.local",
            first=first["TH"],
            start=(15, 45),
            end=(16, 45),
            summary="Theo swim",
            byday="TH",
            location="City Pool",
        ),
        series(
            uid="level-grocery-fri@level.local",
            first=first["FR"],
            start=(16, 15),
            end=(16, 50),
            summary="Grocery run",
            byday="FR",
        ),
        series(
            uid="level-grocery-sun@level.local",
            first=first["SU"],
            start=(10, 0),
            end=(11, 15),
            summary="Grocery + meal prep",
            byday="SU",
        ),
        series(
            uid="level-house-reset@level.local",
            first=first["SA"],
            start=(9, 0),
            end=(10, 0),
            summary="House reset / laundry",
            byday="SA",
            interval=2,
        ),
        # Labor Day + coverage for cancelled pickup / offsite
        once(
            uid="level-labor-helen@level.local",
            day=LABOR_DAY,
            start=(12, 0),
            end=(14, 0),
            summary="Helen lunch visit",
        ),
        once(
            uid="level-labor-park@level.local",
            day=LABOR_DAY,
            start=(16, 0),
            end=(17, 0),
            summary="Park with Nova and Theo",
        ),
        once(
            uid="level-work-offsite-am@level.local",
            day=OFFSITE,
            start=(9, 0),
            end=(12, 0),
            summary="Work",
            location="Home",
        ),
    ]

    one_offs: list[tuple[str, date, tuple[int, int], tuple[int, int], str, str | None]] = [
        ("pharmacy", date(2026, 8, 7), (8, 30), (9, 0), "Helen pharmacy pickup", "Harbor Pharmacy"),
        ("nova-dentist", date(2026, 8, 12), (16, 0), (16, 40), "Nova dentist", "Smile Kids Dental"),
        ("plumber", date(2026, 8, 14), (10, 0), (11, 30), "Plumber - kitchen sink", "Home"),
        ("soccer-parents", date(2026, 8, 18), (18, 0), (19, 0), "Theo soccer parent meeting", "Harbor Field"),
        ("early-pickup", date(2026, 8, 21), (12, 30), (13, 0), "Nova early dismissal pickup", "Riverside Elementary"),
        ("theo-well", date(2026, 8, 24), (16, 15), (16, 50), "Theo well-check", "Riverside Pediatrics"),
        ("helen-followup", date(2026, 8, 26), (14, 0), (14, 40), "Helen follow-up", "Bayside PT Clinic"),
        ("family-dinner", date(2026, 8, 28), (18, 30), (20, 0), "Family dinner - Nova's friend over", "Home"),
        ("back-to-school", date(2026, 9, 1), (15, 30), (16, 15), "Nova back-to-school night", "Riverside Elementary"),
        ("helen-cardio", date(2026, 9, 4), (13, 0), (14, 0), "Helen cardiology", "Pacific Heart"),
        ("nova-dentist-2", date(2026, 9, 8), (15, 40), (16, 20), "Nova dentist follow-up", "Smile Kids Dental"),
        ("class-party", date(2026, 9, 10), (14, 0), (15, 15), "Nova class party (early pickup)", "Riverside Elementary"),
        ("my-dentist", date(2026, 9, 11), (8, 30), (9, 0), "My dentist", "Downtown Dental"),
        ("offsite", OFFSITE, (12, 0), (16, 0), "Work offsite", "Downtown office"),
        ("helen-meds", date(2026, 9, 16), (17, 30), (18, 15), "Helen grocery + meds", "Harbor Pharmacy"),
        ("curriculum", date(2026, 9, 17), (18, 0), (19, 30), "Curriculum night - Theo's class", "Riverside Elementary"),
    ]
    for slug, day, start, end, summary, loc in one_offs:
        events.append(
            once(
                uid=f"level-{slug}@level.local",
                day=day,
                start=start,
                end=end,
                summary=summary,
                location=loc,
            )
        )
    return events


def main() -> Path:
    events = build_events()
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PROD_ID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{CAL_NAME}",
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
        *events,
        "END:VCALENDAR",
        "",
    ]
    out = Path(__file__).resolve().parents[4] / "docs" / "caregiver-month.ics"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\r\n".join(body), encoding="utf-8")
    return out


if __name__ == "__main__":
    path = main()
    print(f"Wrote {path} ({path.stat().st_size} bytes)")
    text = path.read_text()
    print(f"series: {text.count('RRULE:')}")
    print(f"events: {text.count('BEGIN:VEVENT')}")
