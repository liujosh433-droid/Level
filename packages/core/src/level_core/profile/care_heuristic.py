"""Opt-in regex Care infer — tests and LEVEL_ALLOW_HEURISTIC_CARE only.

Live inference uses Gemini. This module must not become a production invent path.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

from level_core.profile.care_feedback import merge_role_feedback
from level_core.profile.people_usuals import hydrate_people_from_roles
from level_core.schemas.care import (
    CARE_ROLE_LABELS,
    CareProfile,
    CareRoleId,
    CareRoleState,
    ProtectedWindow,
)
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import Fact, FactType

_NO_COPARENT_RE = re.compile(
    r"(?:"
    r"\b(?:no|without|don'?t have|do not have|haven'?t got)\s+(?:a\s+)?"
    r"(?:co-?parent|partner|spouse|husband|wife)\b|"
    r"\bthere(?:'s| is| are)\s+no\s+(?:co-?parent|partner)\b|"
    r"\b(?:i'?m|i am)\s+(?:a\s+)?(?:solo|single)\s+parent\b|"
    r"\b(?:solo|single)\s+parent(?:ing)?\b|"
    r"\b(?:only|sole)\s+parent\b|"
    r"\b(?:not|no)\s+co-?parenting\b|"
    r"\bdon'?t\s+co-?parent\b|"
    r"\bno\s+one\s+(?:to\s+)?(?:share|help)\s+(?:with\s+)?(?:parenting|custody|handoffs?)\b"
    r")",
    re.I,
)
_YES_COPARENT_RE = re.compile(
    r"\b(co-?parent|partner|custody|handoff|ex[- ]?partner|my ex)\b",
    re.I,
)


def _event_hour(start: str | None) -> int | None:
    if not start or "T" not in start:
        return None
    try:
        return int(start.split("T", 1)[1][:2])
    except (ValueError, IndexError):
        return None


def _event_weekday(start: str | None) -> int | None:
    if not start:
        return None
    try:
        raw = start.replace("Z", "+00:00")
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw + "T12:00:00+00:00")
        return dt.weekday()
    except ValueError:
        return None


def _fmt_hour(hour: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}{suffix}"


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# --- Person-name inference (calendar + notes + Memory Bank) -----------------

_KID_CUE_RE = re.compile(
    r"school|pickup|drop.?off|soccer|practice|game|swim|ballet|daycare|pediatric|"
    r"teacher|dentist|pediatrician",
    re.I,
)
_ELDER_RE = re.compile(r"\b(mom|dad|mother|father|grandma|grandpa|nan|pop)\b", re.I)
_COPARENT_RE = re.compile(
    r"\b(co-?parent|handoff|custody|exchange|with (ex|dad|mom))\b",
    re.I,
)
_DASH_NAME_RE = re.compile(r"\s+[—\-–]\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*$")
_POSSESSIVE_NAME_RE = re.compile(r"\b([A-Z][a-z]+)'s\b")
_WITH_FOR_NAME_RE = re.compile(r"\b(?:with|for)\s+([A-Z][a-z]+)\b")
_LEADING_NAME_RE = re.compile(
    r"^([A-Z][a-z]+)\s+(?=.+(?:school|pickup|soccer|practice|game|swim|ballet|daycare|dentist))",
)
_NAME_STOP = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "school",
        "soccer",
        "practice",
        "pickup",
        "dropoff",
        "work",
        "office",
        "meeting",
        "sync",
        "standup",
        "clinic",
        "doctor",
        "dentist",
        "therapy",
        "ultrasound",
        "hospital",
        "class",
        "game",
        "swim",
        "ballet",
        "daycare",
        "pediatric",
        "teacher",
        "forms",
        "errand",
        "grocery",
        "package",
        "pharmacy",
        "insurance",
        "interview",
        "sprint",
        "client",
        "shift",
        "staff",
        "hands",
        "payroll",
        "all",
        "the",
        "and",
        "with",
        "for",
        "kids",
        "kid",
        "child",
        "children",
        "family",
        "team",
        "zoom",
        "google",
        "calendar",
        "appt",
        "appointment",
        "check",
        "weekly",
        "daily",
        "morning",
        "evening",
        "lunch",
        "dinner",
        "home",
        "away",
        "virtual",
        "call",
        "chat",
        "email",
        "urgent",
        "follow",
        "review",
        "planning",
        "project",
        "status",
        "update",
        "block",
        "focus",
        "deep",
        "time",
        "care",
        "block",
    }
)


def _is_person_name(name: str | None) -> bool:
    if not name or len(name) < 2:
        return False
    if name.lower() in _NAME_STOP:
        return False
    return bool(re.fullmatch(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?", name))


def _union_people(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for name in group:
            key = name.lower()
            if key in seen or not _is_person_name(name):
                continue
            seen.add(key)
            out.append(name)
    return out[:4]


def extract_people_from_calendar_title(summary: str) -> tuple[list[str], list[str], list[str]]:
    """Return (child_names, elder_names, partner_names) from one calendar title.

    Names are optional — kid cues without a person still signal child care elsewhere.
    """
    summary = " ".join(summary.split()).strip()
    children: list[str] = []
    elders: list[str] = []
    partners: list[str] = []
    if not summary:
        return children, elders, partners

    dash = _DASH_NAME_RE.search(summary)
    dash_name = dash.group(1) if dash else None
    title = summary[: dash.start()].strip(" —-–") if dash else summary
    kidish = bool(_KID_CUE_RE.search(summary))

    if dash_name and _is_person_name(dash_name):
        if kidish or _KID_CUE_RE.search(title):
            children.append(dash_name)
        elif _ELDER_RE.search(dash_name):
            elders.append(dash_name.title() if dash_name.islower() else dash_name)
        elif _COPARENT_RE.search(summary):
            partners.append(dash_name)

    if kidish:
        for m in _POSSESSIVE_NAME_RE.finditer(summary):
            if _is_person_name(m.group(1)):
                children.append(m.group(1))
        for m in _WITH_FOR_NAME_RE.finditer(summary):
            if _is_person_name(m.group(1)) and not _ELDER_RE.search(m.group(1)):
                children.append(m.group(1))
        lead = _LEADING_NAME_RE.match(summary)
        if lead and _is_person_name(lead.group(1)):
            children.append(lead.group(1))

    elder_m = _ELDER_RE.search(summary)
    if elder_m:
        elders.append(elder_m.group(1).title())

    if _COPARENT_RE.search(summary):
        for m in _WITH_FOR_NAME_RE.finditer(summary):
            name = m.group(1)
            if _is_person_name(name) and name.lower() not in {"ex", "dad", "mom"}:
                partners.append(name)
        # "Handoff — Alex" / custody with a CapitalizedName
        if dash_name and _is_person_name(dash_name) and dash_name not in partners:
            if not _ELDER_RE.search(dash_name) and dash_name not in children:
                partners.append(dash_name)

    return _union_people(children), _union_people(elders), _union_people(partners)


def people_mentions_from_facts(facts: list[Fact]) -> dict[CareRoleId, list[str]]:
    """Pull person anchors from Memory Bank relationship / value / commitment facts."""
    child_re = [
        re.compile(
            r"\b(?:my|our)\s+(?:kid|child|daughter|son)\s+([A-Z][a-z]+)\b",
            re.I,
        ),
        re.compile(
            r"\b([A-Z][a-z]+)(?:'s)?\s+(?:school|pickup|soccer|daycare|dentist|practice)\b",
        ),
        re.compile(
            r"\b(?:with|for)\s+([A-Z][a-z]+)\b.{0,40}\b(?:school|pickup|kid|child|soccer)\b",
            re.I,
        ),
        re.compile(r"\bday-to-day with ([A-Z][a-z]+)\b", re.I),
        re.compile(r"\bChild care[^\n]{0,60}\bwith ([A-Z][a-z]+)\b", re.I),
        re.compile(r"\bprotecting[^\n]{0,40}\bwith ([A-Z][a-z]+)\b", re.I),
    ]
    partner_re = [
        re.compile(r"\b(?:partner|co-?parent|ex)\s+([A-Z][a-z]+)\b", re.I),
        re.compile(r"\b([A-Z][a-z]+)\s+(?:is\s+)?(?:my\s+)?(?:partner|co-?parent)\b", re.I),
        re.compile(r"\bhandoffs?\s+with\s+([A-Z][a-z]+)\b", re.I),
    ]
    out: dict[CareRoleId, list[str]] = {
        CareRoleId.CHILD_CARE: [],
        CareRoleId.ELDER_CARE: [],
        CareRoleId.PARTNER_COPARENT: [],
    }
    allowed = {
        FactType.RELATIONSHIP,
        FactType.VALUE_STATEMENT,
        FactType.COMMITMENT,
        FactType.PREFERENCE,
        FactType.CONSTRAINT,
    }
    for fact in facts:
        if fact.type not in allowed:
            continue
        text = fact.statement or ""
        for pat in child_re:
            for m in pat.finditer(text):
                name = next((g for g in m.groups() if g), None)
                if not name:
                    continue
                cleaned = name[:1].upper() + name[1:].lower() if name else ""
                # Prefer original capitalization when already Title Case
                if name[:1].isupper() and (len(name) == 1 or name[1:].islower()):
                    cleaned = name
                if _is_person_name(cleaned):
                    out[CareRoleId.CHILD_CARE].append(cleaned)
        elder_m = _ELDER_RE.search(text)
        if elder_m:
            out[CareRoleId.ELDER_CARE].append(elder_m.group(1).title())
        close = re.search(r"\bstaying close with ([A-Z][a-z]+)\b", text, re.I)
        if close:
            cleaned = close.group(1).title()
            if _is_person_name(cleaned):
                out[CareRoleId.ELDER_CARE].append(cleaned)
        for pat in partner_re:
            for m in pat.finditer(text):
                name = next((g for g in m.groups() if g), None)
                if not name:
                    continue
                cleaned = name.title()
                if _is_person_name(cleaned):
                    out[CareRoleId.PARTNER_COPARENT].append(cleaned)
    return {
        rid: _union_people(names)
        for rid, names in out.items()
        if names
    }


def people_from_note(note: str) -> dict[CareRoleId, list[str]]:
    """Extract named people from a free-text Tell Level note."""
    text = note.strip()
    if not text:
        return {}
    out: dict[CareRoleId, list[str]] = {}
    child_pats = [
        re.compile(
            r"\b(?:my|our)\s+(?:kid|child|daughter|son)\s+(?:is\s+|named\s+)?([A-Za-z]+)\b",
            re.I,
        ),
        re.compile(
            r"\b(?:kid|child|daughter|son)\s+(?:is\s+|named\s+)([A-Za-z]+)\b",
            re.I,
        ),
    ]
    partner_pats = [
        re.compile(r"\b(?:partner|co-?parent|ex)\s+([A-Za-z]+)\b", re.I),
        re.compile(r"\b([A-Za-z]+)\s+is\s+my\s+(?:partner|co-?parent|ex)\b", re.I),
    ]
    children: list[str] = []
    for pat in child_pats:
        for m in pat.finditer(text):
            cleaned = m.group(1).title()
            if _is_person_name(cleaned):
                children.append(cleaned)
    if children:
        out[CareRoleId.CHILD_CARE] = _union_people(children)
    elders: list[str] = []
    for m in _ELDER_RE.finditer(text):
        elders.append(m.group(1).title())
    if elders:
        out[CareRoleId.ELDER_CARE] = _union_people(elders)
    partners: list[str] = []
    for pat in partner_pats:
        for m in pat.finditer(text):
            cleaned = m.group(1).title()
            if _is_person_name(cleaned):
                partners.append(cleaned)
    if partners:
        out[CareRoleId.PARTNER_COPARENT] = _union_people(partners)
    return out


def merge_people_into_care_profile(
    profile: CareProfile,
    mentions: dict[CareRoleId, list[str]],
) -> CareProfile:
    """Union person anchors into matching roles; create thin roles if missing."""
    if not mentions:
        return hydrate_people_from_roles(profile)
    by_id = {r.role_id: r for r in profile.roles}
    changed = False
    for role_id, names in mentions.items():
        if not names:
            continue
        existing = by_id.get(role_id)
        if existing is None:
            by_id[role_id] = CareRoleState(
                role_id=role_id,
                label=CARE_ROLE_LABELS[role_id],
                salience=0.72,
                weekly_load_hours=2.0,
                status=BulletStatus.PENDING,
                people=names[:4],
                evidence_summaries=[f"Learned people: {', '.join(names[:4])}"],
            )
            changed = True
            continue
        merged = _union_people(existing.people, names)
        if merged != existing.people:
            by_id[role_id] = existing.model_copy(update={"people": merged})
            changed = True
    if not changed:
        return hydrate_people_from_roles(profile)
    updated = profile.model_copy(
        update={
            "roles": list(by_id.values()),
            "version": profile.version + 1,
            "updated_at": datetime.now(tz=timezone.utc),
        }
    )
    return hydrate_people_from_roles(updated)


def classify_calendar_event(summary: str) -> CareRoleId | None:
    """LEGACY regex classifier — tests / ``LEVEL_ALLOW_HEURISTIC_CARE`` only.

    Live paths use AI ``calendar_role_by_summary``. Do not call this for product
    intelligence; see :mod:`level_core.profile.ai_wrappers`.
    """
    text = " ".join((summary or "").split()).strip()
    if not text or text == "(no title)":
        return None
    children, elders, partners = extract_people_from_calendar_title(text)
    if children or _KID_CUE_RE.search(text):
        return CareRoleId.CHILD_CARE
    if elders or _ELDER_RE.search(text):
        return CareRoleId.ELDER_CARE
    if partners or _COPARENT_RE.search(text):
        return CareRoleId.PARTNER_COPARENT
    # Coarse offline cues only — ambiguous titles stay uncategorized for AI.
    if re.search(
        r"\b(night\s+class|evening\s+class|class(es)?|course|lecture|seminar|"
        r"certification|bootcamp|workshop)\b",
        text,
        re.I,
    ):
        return None
    if re.search(
        r"\b(work|standup|stand-up|1:1|one-on-one|interview|sprint|office|shift|"
        r"sync|staff meeting|all[\s-]?hands|payroll|client|meeting|zoom|call|"
        r"conference|deadline|okrs?|perf review|performance review)\b",
        text,
        re.I,
    ):
        return CareRoleId.PAID_WORK
    if re.search(
        r"\b(therapy|counsel(ing|ling)?|massage|pt\b|physio|mental health|"
        r"recovery|sleep|rest|wind.?down|bedtime|meditat)\b",
        text,
        re.I,
    ):
        return CareRoleId.SELF_RECOVERY
    # Caregiver's own medical appts (not labeled for Mom/kid) → recovery-ish.
    if re.search(
        r"\b(dentist|doctor|clinic|ultrasound|hospital|appt|appointment)\b",
        text,
        re.I,
    ) and not elders and not children:
        return CareRoleId.SELF_RECOVERY
    if re.search(
        r"\b(form|paperwork|dmv|insurance|pediatric forms|permission slip|supply|"
        r"grocery|errand|dry clean|package|pharmacy)\b",
        text,
        re.I,
    ):
        return CareRoleId.HOUSEHOLD_LOGISTICS
    return None

def _reject_care_role(
    by_id: dict[CareRoleId, CareRoleState],
    role_id: CareRoleId,
    note: str,
) -> None:
    """Persist an explicit Not-me so re-inference and the graph omit this role."""
    evidence = note.strip()[:200]
    existing = by_id.get(role_id)
    if existing is None:
        by_id[role_id] = CareRoleState(
            role_id=role_id,
            label=CARE_ROLE_LABELS[role_id],
            salience=0.1,
            weekly_load_hours=0.0,
            status=BulletStatus.REJECTED,
            evidence_summaries=[evidence] if evidence else [],
            people=[],
        )
        return
    by_id[role_id] = existing.model_copy(
        update={
            "status": BulletStatus.REJECTED,
            "salience": min(existing.salience, 0.2),
            "weekly_load_hours": 0.0,
            "people": [],
            "evidence_summaries": ([evidence, *existing.evidence_summaries][:4] if evidence else existing.evidence_summaries),
        }
    )


def adjust_care_profile_from_note(profile: CareProfile, note: str) -> CareProfile:
    """LEGACY keyword upsert — tests / opt-in only. Prefer AI note apply."""
    text = note.lower()
    rejects: list[CareRoleId] = []
    if _NO_COPARENT_RE.search(text):
        rejects.append(CareRoleId.PARTNER_COPARENT)

    bumps: list[tuple[CareRoleId, float]] = []
    if re.search(r"\b(kid|child|school|pickup|soccer|daughter|son)\b", text):
        bumps.append((CareRoleId.CHILD_CARE, 0.08))
    if re.search(r"\b(mom|dad|mother|father|grandma|grandpa)\b", text):
        bumps.append((CareRoleId.ELDER_CARE, 0.08))
    if re.search(r"\b(work|job|promotion|meeting|office|shift)\b", text):
        bumps.append((CareRoleId.PAID_WORK, 0.06))
    if re.search(r"\b(sleep|therapy|health|rest|burnout|tired)\b", text):
        bumps.append((CareRoleId.SELF_RECOVERY, 0.08))
    # Affirm co-parent only when the note is not a negation ("no co-parent").
    if CareRoleId.PARTNER_COPARENT not in rejects and _YES_COPARENT_RE.search(text):
        bumps.append((CareRoleId.PARTNER_COPARENT, 0.08))
    if re.search(r"\b(form|paperwork|appointment|logistics|errand)\b", text):
        bumps.append((CareRoleId.HOUSEHOLD_LOGISTICS, 0.06))
    named = people_from_note(note)
    if CareRoleId.PARTNER_COPARENT in rejects:
        named.pop(CareRoleId.PARTNER_COPARENT, None)
    if not bumps and not named and not rejects:
        return profile
    by_id = {r.role_id: r for r in profile.roles}
    for role_id in rejects:
        _reject_care_role(by_id, role_id, note)
    for role_id, delta in bumps:
        if role_id in rejects:
            continue
        existing = by_id.get(role_id)
        # Keep / Not me wins: keyword bumps must not un-reject a role.
        if existing is not None and existing.status is BulletStatus.REJECTED:
            continue
        extra_people = named.get(role_id, [])
        if existing is None:
            by_id[role_id] = CareRoleState(
                role_id=role_id,
                label=CARE_ROLE_LABELS[role_id],
                salience=min(0.9, 0.7 + delta),
                weekly_load_hours=2.0,
                status=BulletStatus.ACCEPTED,
                evidence_summaries=[note.strip()[:200]],
                people=extra_people[:4],
            )
        else:
            by_id[role_id] = existing.model_copy(
                update={
                    "salience": min(0.98, existing.salience + delta),
                    "status": existing.status,
                    "evidence_summaries": [note.strip()[:200], *existing.evidence_summaries][:4],
                    "people": _union_people(existing.people, extra_people),
                }
            )
    # Named people for roles not bumped by keywords
    for role_id, names in named.items():
        existing = by_id.get(role_id)
        if existing is not None and existing.status is BulletStatus.REJECTED:
            continue
        if existing is not None:
            merged = _union_people(existing.people, names)
            if merged != existing.people:
                by_id[role_id] = existing.model_copy(update={"people": merged})
        else:
            by_id[role_id] = CareRoleState(
                role_id=role_id,
                label=CARE_ROLE_LABELS[role_id],
                salience=0.75,
                weekly_load_hours=2.0,
                status=BulletStatus.ACCEPTED,
                evidence_summaries=[note.strip()[:200]],
                people=names[:4],
            )
    updated = profile.model_copy(
        update={
            "roles": list(by_id.values()),
            "version": profile.version + 1,
            "updated_at": datetime.now(tz=timezone.utc),
        }
    )
    return hydrate_people_from_roles(updated)


def infer_care_profile_heuristic(
    events: list[dict[str, str | None]],
    *,
    user_id: str,
    previous: CareProfile | None = None,
) -> tuple[CareProfile, list[Fact]]:
    """OFFLINE FALLBACK only — regex/static calendar scoring.

    Prefer :func:`level_core.profile.care_infer_llm.infer_care_profile_ai`.
    """
    return _infer_care_profile_heuristic_impl(events, user_id=user_id, previous=previous)


def infer_care_profile(
    events: list[dict[str, str | None]],
    *,
    user_id: str,
    previous: CareProfile | None = None,
) -> tuple[CareProfile, list[Fact]]:
    """Deprecated alias for the heuristic fallback (tests / offline)."""
    return infer_care_profile_heuristic(events, user_id=user_id, previous=previous)


def _infer_care_profile_heuristic_impl(
    events: list[dict[str, str | None]],
    *,
    user_id: str,
    previous: CareProfile | None = None,
) -> tuple[CareProfile, list[Fact]]:
    """Infer caregiver Care Profile + durable facts from calendar events."""
    if not events:
        empty = previous or CareProfile(user_id=user_id, roles=[])
        return empty, []

    work_re = re.compile(
        r"\b(work|standup|stand-up|1:1|one-on-one|interview|sprint|office|shift|"
        r"sync|staff meeting|all[\s-]?hands|payroll|client|meeting|zoom|call|"
        r"conference|deadline|okrs?)\b",
        re.I,
    )
    # Rest/wellbeing only — never adult classes/courses.
    health_re = re.compile(
        r"\b(therapy|counsel(ing|ling)?|massage|pt\b|physio|mental health|"
        r"recovery|meditat)\b",
        re.I,
    )
    sleep_re = re.compile(r"\b(sleep|rest|wind.?down|bedtime)\b", re.I)
    class_re = re.compile(
        r"\b(night\s+class|evening\s+class|class(es)?|course|lecture|seminar|"
        r"certification|bootcamp|workshop)\b",
        re.I,
    )
    logistics_re = re.compile(
        r"\b(form|paperwork|dmv|insurance|pediatric forms|permission slip|supply|"
        r"grocery|errand|dry clean|package|pharmacy)\b",
        re.I,
    )

    child_events: Counter[str] = Counter()
    child_hours: list[tuple[int | None, int | None, str]] = []
    elder_hits: Counter[str] = Counter()
    partner_hits: Counter[str] = Counter()
    work_hours: list[int] = []
    health_n = 0
    sleep_n = 0
    logistics_n = 0
    coparent_n = 0
    early_n = 0
    late_n = 0
    anon_kid = 0

    for ev in events:
        summary = " ".join(((ev.get("summary") or "")).split()).strip()
        if not summary or summary == "(no title)":
            continue
        hour = _event_hour(ev.get("start"))
        weekday = _event_weekday(ev.get("start"))
        if hour is not None:
            if hour < 7:
                early_n += 1
            if hour >= 20:
                late_n += 1

        children, elders, partners = extract_people_from_calendar_title(summary)
        kidish = bool(_KID_CUE_RE.search(summary))
        if children:
            for person in children:
                child_events[person] += 1
                child_hours.append((weekday, hour, summary))
        elif kidish:
            anon_kid += 1
            child_hours.append((weekday, hour, summary))

        for person in elders:
            elder_hits[person] += 1
        for person in partners:
            partner_hits[person] += 1

        if class_re.search(summary) and not kidish and not children:
            # Adult class/course: never recovery. Career-flavored → paid work load.
            if re.search(
                r"\b(work|job|career|professional|cert|license|training|pmp|mba)\b",
                summary,
                re.I,
            ):
                if hour is not None:
                    work_hours.append(hour)
                else:
                    work_hours.append(19)
        elif work_re.search(summary):
            if hour is not None and 7 <= hour <= 18:
                work_hours.append(hour)
            elif hour is None:
                work_hours.append(9)
        if health_re.search(summary) and not class_re.search(summary):
            health_n += 1
        if sleep_re.search(summary):
            sleep_n += 1
        if logistics_re.search(summary):
            logistics_n += 1
        if _COPARENT_RE.search(summary) or partners:
            coparent_n += 1

    facts: list[Fact] = []
    roles: list[CareRoleState] = []

    def _fact(
        statement: str,
        *,
        salience: float,
        ftype: FactType = FactType.VALUE_STATEMENT,
    ) -> Fact:
        fact = Fact(
            user_id=user_id,
            type=ftype,
            statement=statement,
            salience=salience,
            confidence=0.78,
            source_signal_ids=[],
            written_by="care_roles@v1",
        )
        facts.append(fact)
        return fact

    # --- child_care ---
    people = [name for name, n in child_events.most_common(3) if n >= 2]
    kid_n = sum(child_events[p] for p in people) if people else sum(
        1 for _n in child_events.values() if _n >= 1
    )
    if people or kid_n >= 2 or anon_kid >= 3:
        if not people and child_events:
            people = [name for name, n in child_events.most_common(2) if n >= 1]
        who = ", ".join(people) if people else "your kids"
        stmt = (
            f"Child care — holding school, sports, and day-to-day with {who}"
        )
        fact = _fact(stmt, salience=0.92, ftype=FactType.RELATIONSHIP)
        windows: list[ProtectedWindow] = []
        for weekday, hour, summary in child_hours:
            if hour is None:
                continue
            if 14 <= hour <= 18 or _KID_CUE_RE.search(summary or ""):
                day = _WEEKDAYS[weekday] if weekday is not None else "weekday"
                windows.append(
                    ProtectedWindow(
                        label=f"{day} ~{_fmt_hour(hour)} care block",
                        weekday=weekday,
                        start_hour=hour,
                        end_hour=min(hour + 2, 23),
                        evidence=(summary or "")[:120],
                    )
                )
            if len(windows) >= 3:
                break
        role = CareRoleState(
            role_id=CareRoleId.CHILD_CARE,
            label=CARE_ROLE_LABELS[CareRoleId.CHILD_CARE],
            salience=0.92,
            weekly_load_hours=float(min(40.0, max(3.0, (kid_n + anon_kid) * 1.5))),
            protected_windows=windows,
            source_fact_ids=[fact.fact_id],
            evidence_summaries=[stmt],
            people=people,
        )
        roles.append(merge_role_feedback(role, previous))

    # --- elder_care ---
    for name, n in elder_hits.most_common(2):
        if n < 1:
            continue
        stmt = f"Elder care — staying close with {name}; visits and check-ins matter"
        fact = _fact(stmt, salience=0.84, ftype=FactType.RELATIONSHIP)
        role = CareRoleState(
            role_id=CareRoleId.ELDER_CARE,
            label=CARE_ROLE_LABELS[CareRoleId.ELDER_CARE],
            salience=0.84,
            weekly_load_hours=float(min(20.0, n * 2.0)),
            source_fact_ids=[fact.fact_id],
            evidence_summaries=[stmt],
            people=[name],
        )
        roles.append(merge_role_feedback(role, previous))
        break  # one elder_care role aggregating top person

    # --- paid_work ---
    if len(work_hours) >= 1:
        if len(work_hours) >= 3:
            start_h = min(work_hours)
            end_h = max(work_hours) + 1
            if end_h - start_h < 3:
                start_h, end_h = 9, 17
            start_h = max(7, min(start_h, 11))
            end_h = max(start_h + 4, min(end_h, 19))
            stmt = (
                f"Work/Job — weekday focus roughly {_fmt_hour(start_h)}–{_fmt_hour(end_h)} stays held"
            )
            sal = 0.88
            windows = [
                ProtectedWindow(
                    label=f"Weekday {_fmt_hour(start_h)}–{_fmt_hour(end_h)} work",
                    start_hour=start_h,
                    end_hour=end_h,
                )
            ]
        else:
            stmt = "Work/Job — holding focused work time during the week"
            sal = 0.75
            windows = []
        fact = _fact(stmt, salience=sal, ftype=FactType.COMMITMENT)
        role = CareRoleState(
            role_id=CareRoleId.PAID_WORK,
            label=CARE_ROLE_LABELS[CareRoleId.PAID_WORK],
            salience=sal,
            weekly_load_hours=float(min(50.0, max(8.0, len(work_hours) * 2.0))),
            protected_windows=windows,
            source_fact_ids=[fact.fact_id],
            evidence_summaries=[stmt],
        )
        roles.append(merge_role_feedback(role, previous))

    # --- self_recovery ---
    if health_n >= 1 or sleep_n >= 1 or (early_n >= 2 and late_n >= 2):
        if health_n >= 2 or sleep_n >= 1:
            stmt = (
                "Self & recovery — health appointments and rest stay on the calendar"
            )
            sal = 0.8
        else:
            stmt = "Self & recovery — the week shouldn’t run you into the ground"
            sal = 0.72
        fact = _fact(stmt, salience=sal)
        role = CareRoleState(
            role_id=CareRoleId.SELF_RECOVERY,
            label=CARE_ROLE_LABELS[CareRoleId.SELF_RECOVERY],
            salience=sal,
            weekly_load_hours=float(min(15.0, (health_n + sleep_n) * 1.5 + 2.0)),
            source_fact_ids=[fact.fact_id],
            evidence_summaries=[stmt],
        )
        roles.append(merge_role_feedback(role, previous))

    # --- household_logistics ---
    if logistics_n >= 2:
        stmt = "Household logistics — forms, errands, and ops glue that keep care running"
        fact = _fact(stmt, salience=0.7, ftype=FactType.CONSTRAINT)
        role = CareRoleState(
            role_id=CareRoleId.HOUSEHOLD_LOGISTICS,
            label=CARE_ROLE_LABELS[CareRoleId.HOUSEHOLD_LOGISTICS],
            salience=0.7,
            weekly_load_hours=float(min(12.0, logistics_n * 1.25)),
            source_fact_ids=[fact.fact_id],
            evidence_summaries=[stmt],
        )
        roles.append(merge_role_feedback(role, previous))

    # --- partner_coparent ---
    if coparent_n >= 1:
        partner_people = [name for name, n in partner_hits.most_common(2) if n >= 1]
        who = f" with {', '.join(partner_people)}" if partner_people else ""
        stmt = (
            f"Co-parent / partner — handoffs and coordination{who} are part of the care load"
        )
        fact = _fact(stmt, salience=0.78, ftype=FactType.RELATIONSHIP)
        role = CareRoleState(
            role_id=CareRoleId.PARTNER_COPARENT,
            label=CARE_ROLE_LABELS[CareRoleId.PARTNER_COPARENT],
            salience=0.78,
            weekly_load_hours=float(min(10.0, coparent_n * 2.0)),
            source_fact_ids=[fact.fact_id],
            evidence_summaries=[stmt],
            people=partner_people,
        )
        roles.append(merge_role_feedback(role, previous))

    # Deduplicate facts
    seen: set[str] = set()
    unique: list[Fact] = []
    for fact in facts:
        key = fact.statement.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(fact)

    version = (previous.version + 1) if previous else 1
    profile = CareProfile(
        user_id=user_id,
        roles=roles,
        version=version,
        updated_at=datetime.now(tz=timezone.utc),
        conflict_summaries=list(previous.conflict_summaries) if previous else [],
    )
    return hydrate_people_from_roles(profile), unique[:8]

