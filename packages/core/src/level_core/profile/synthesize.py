"""Build durable profile snapshot + manifesto + contradictions from facts."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from level_core.schemas.bias import Manifesto
from level_core.schemas.profile import (
    BulletCategory,
    BulletStatus,
    Contradiction,
    ProfileBullet,
    ProfileSnapshot,
)
from level_core.schemas.signal import Fact, FactType

_TOKEN = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "have",
        "will",
        "want",
        "need",
        "about",
        "into",
        "their",
        "them",
        "been",
        "were",
        "when",
        "what",
        "your",
        "you",
        "are",
        "was",
        "not",
        "but",
        "can",
        "our",
        "out",
        "all",
    }
)

_TYPE_TO_CATEGORY = {
    FactType.VALUE_STATEMENT: BulletCategory.PRIORITY,
    FactType.COMMITMENT: BulletCategory.PRIORITY,
    FactType.CONSTRAINT: BulletCategory.CONSTRAINT,
    FactType.RELATIONSHIP: BulletCategory.PRIORITY,
    FactType.PREFERENCE: BulletCategory.PRIORITY,
    FactType.EVENT: BulletCategory.LOAD,
}

# Calendar analytics — never surface these as "who you are" priorities.
_ANALYTICS_STATEMENT = re.compile(
    r"("
    r"on my calendar|"
    r"in (this|the) (current )?window|"
    r"appears? repeatedly|"
    r"shows? up (often|repeatedly)|"
    r"keeps? recurring|"
    r"times recently|"
    r"part of my (regular |normal )?load|"
    r"frequent evening|"
    r"\d+\s+in (this|the)"
    r")",
    re.I,
)

# Soft opposition cues for contradiction pairing.
_POS = re.compile(
    r"\b(will|plan to|going to|committed|promise|protect|prioritize|want to)\b",
    re.I,
)
_NEG = re.compile(
    r"\b(can'?t|cannot|won'?t|unable|miss|sacrifice|late shift|overwhelm|too much|no time)\b",
    re.I,
)


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def _tokens(text: str) -> set[str]:
    return {_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP}


def detect_contradictions(facts: list[Fact], *, user_id: str, limit: int = 6) -> list[Contradiction]:
    """Heuristic pairs: commitments/values vs constraints/concerns with shared topic tokens."""
    side_a = [
        f
        for f in facts
        if f.type in {FactType.COMMITMENT, FactType.VALUE_STATEMENT, FactType.PREFERENCE}
    ]
    side_b = [
        f
        for f in facts
        if f.type in {FactType.CONSTRAINT, FactType.CONCERN, FactType.DECISION_HISTORY}
    ]
    out: list[Contradiction] = []
    seen: set[tuple[str, str]] = set()
    for a in side_a:
        ta = _tokens(a.statement)
        if len(ta) < 2:
            continue
        a_pos = bool(_POS.search(a.statement))
        for b in side_b:
            if a.fact_id == b.fact_id:
                continue
            tb = _tokens(b.statement)
            overlap = ta & tb
            b_neg = bool(_NEG.search(b.statement)) or b.type in {
                FactType.CONSTRAINT,
                FactType.CONCERN,
            }
            # Prefer pairs that look like pull in different directions.
            opposing = a_pos and b_neg
            if len(overlap) < (1 if opposing else 2):
                continue
            if not (a_pos or b_neg):
                continue
            key = tuple(sorted((a.fact_id, b.fact_id)))
            if key in seen:
                continue
            seen.add(key)
            topic = " ".join(sorted(overlap)[:3])
            out.append(
                Contradiction(
                    user_id=user_id,
                    topic=topic[:80],
                    fact_id_a=a.fact_id,
                    fact_id_b=b.fact_id,
                    summary=f"Possible tension: “{a.statement[:120]}” vs “{b.statement[:120]}”",
                )
            )
            if len(out) >= limit:
                return out
    return out


def _is_analytics_statement(text: str) -> bool:
    return bool(_ANALYTICS_STATEMENT.search(text or ""))


def build_manifesto_statement(facts: list[Fact]) -> tuple[str, list[str]]:
    """Top priorities as a short intro + digestible bullet lines."""
    priorities = [
        f
        for f in facts
        if f.confidence >= 0.5
        and f.type
        in {
            FactType.VALUE_STATEMENT,
            FactType.COMMITMENT,
            FactType.RELATIONSHIP,
            FactType.PREFERENCE,
        }
        and not _is_analytics_statement(f.statement)
    ]
    priorities.sort(key=lambda f: f.salience, reverse=True)
    if not priorities:
        return (
            "Still learning what you protect when the week gets hard.",
            [],
        )
    top = priorities[:3]
    labels = [f.statement.rstrip(".") for f in top]
    lines = ["Right now it looks like you prioritize:", *[f"• {label}" for label in labels]]
    return "\n".join(lines)[:4000], [f.fact_id for f in top]


def synthesize_snapshot(facts: list[Fact], *, user_id: str) -> ProfileSnapshot:
    """Build a short list of inferred priorities the user can Keep / Not me."""
    bullets: list[ProfileBullet] = []
    ranked = sorted(
        [
            f
            for f in facts
            if f.confidence >= 0.55 and not _is_analytics_statement(f.statement)
        ],
        key=lambda f: (
            1 if (f.written_by or "").startswith("agenda_priorities") else 0,
            f.salience,
            f.confidence,
        ),
        reverse=True,
    )
    # Prefer priority-shaped types; keep a couple of real constraints if useful.
    per_cat: dict[BulletCategory, int] = defaultdict(int)
    seen_text: set[str] = set()
    for fact in ranked:
        cat = _TYPE_TO_CATEGORY.get(fact.type)
        if cat is None or cat is BulletCategory.LOAD:
            continue
        # Profile page is priorities-first — soft-cap constraints.
        if cat is BulletCategory.CONSTRAINT and per_cat[cat] >= 1:
            continue
        if cat is BulletCategory.PRIORITY and per_cat[cat] >= 6:
            continue
        if per_cat[cat] >= 4:
            continue
        text = fact.statement.strip()
        key = text.lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        per_cat[cat] += 1
        # Agenda priorities already use PRIORITY; map value/relationship the same.
        if cat in {BulletCategory.VALUE, BulletCategory.RELATIONSHIP, BulletCategory.COMMITMENT}:
            cat = BulletCategory.PRIORITY
        bullets.append(
            ProfileBullet(
                category=cat,
                text=text[:220],
                source_fact_ids=[fact.fact_id],
            )
        )

    contradictions = detect_contradictions(facts, user_id=user_id)
    # Keep tensions on the side — don't pollute the priority list.
    return ProfileSnapshot(
        user_id=user_id,
        bullets=bullets[:8],
        contradictions=contradictions[:4],
        needs_review=True,
        fact_count=len(facts),
    )


def calendar_pattern_facts(
    event_statements: list[str],
    *,
    user_id: str,
) -> list[Fact]:
    """Collapse calendar titles into a few schedule-pattern constraints."""
    if not event_statements:
        return []

    evening = 0
    medical = 0
    travel = 0
    childcareish = 0
    weekday_titles: list[str] = []
    med_re = re.compile(
        r"ultrasound|dentist|doctor|clinic|retainers?|therapy|appt|appointment|hospital",
        re.I,
    )
    travel_re = re.compile(r"\b(flight|trip|travel|japan|airport|hotel)\b", re.I)
    care_re = re.compile(r"school|pickup|daycare|parent|teacher|pediatric", re.I)
    evening_re = re.compile(r"\b(5:|6:|7:|8:|9:).*(PM|pm)|\bevening\b", re.I)

    for stmt in event_statements:
        if evening_re.search(stmt):
            evening += 1
        if med_re.search(stmt):
            medical += 1
        if travel_re.search(stmt):
            travel += 1
        if care_re.search(stmt):
            childcareish += 1
        weekday_titles.append(stmt)

    facts: list[Fact] = []

    def _add(statement: str, salience: float) -> None:
        facts.append(
            Fact(
                user_id=user_id,
                type=FactType.CONSTRAINT,
                statement=statement,
                salience=salience,
                confidence=0.75,
                source_signal_ids=[],
                written_by="calendar_patterns@v1",
            )
        )

    if evening >= 3:
        _add(
            f"My calendar shows frequent evening commitments ({evening} in the current window), "
            "so weeknights are often already loaded.",
            0.7,
        )
    if medical >= 2:
        _add(
            f"I have multiple medical/health appointments on my calendar ({medical} in this window).",
            0.65,
        )
    if travel >= 1:
        _add(
            "I have travel on my calendar in this window, which constrains nearby scheduling.",
            0.6,
        )
    if childcareish >= 2:
        _add(
            "School/child-related events show up repeatedly on my calendar and need protected time.",
            0.7,
        )

    # Dominant unique title themes (non-generic).
    titles = []
    for stmt in event_statements:
        m = re.search(r":\s*(.+)$", stmt)
        titles.append((m.group(1) if m else stmt).split("—")[0].strip().lower())
    counts = Counter(t for t in titles if 3 <= len(t) <= 40)
    for title, n in counts.most_common(3):
        if n >= 2 and not med_re.search(title):
            _add(
                f"“{title}” appears repeatedly on my calendar ({n} times) — it is part of my regular load.",
                0.55,
            )
            break

    return facts


def agenda_life_facts(
    event_summaries: list[str],
    *,
    user_id: str,
) -> list[Fact]:
    """Backward-compatible wrapper — prefer :func:`infer_priority_facts`."""
    events = [{"summary": s, "start": None} for s in event_summaries]
    return infer_priority_facts(events, user_id=user_id)


def _event_hour(start: str | None) -> int | None:
    if not start or "T" not in start:
        return None
    try:
        return int(start.split("T", 1)[1][:2])
    except (ValueError, IndexError):
        return None


def _fmt_hour(hour: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}{suffix}"


def infer_priority_facts(
    events: list[dict[str, str | None]],
    *,
    user_id: str,
) -> list[Fact]:
    """Infer concise life priorities from calendar events (a step beyond event dumps).

    Examples:
    - regular soccer / school with Jordan → family time with Jordan
    - weekday work blocks → protect work hours ~9–5
    - Mom visits → staying close with Mom
    """
    if not events:
        return []

    name_re = re.compile(r"\s+[—\-–]\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*$")
    kid_cue = re.compile(
        r"school|pickup|drop.?off|soccer|practice|game|swim|ballet|daycare|pediatric|teacher",
        re.I,
    )
    elder_re = re.compile(r"\b(mom|dad|mother|father|grandma|grandpa|nan|pop)\b", re.I)
    work_re = re.compile(
        r"\b(work|standup|stand-up|1:1|one-on-one|interview|sprint|office|shift|"
        r"sync|staff meeting|all hands|payroll|client)\b",
        re.I,
    )
    health_re = re.compile(
        r"therapy|counsel|dentist|doctor|clinic|ultrasound|hospital|appt|appointment|"
        r"mental|recovery|massage|pt\b|physio",
        re.I,
    )
    learn_re = re.compile(r"\b(class|course|lecture|study|night class|muay|gym)\b", re.I)
    sleep_re = re.compile(r"\b(sleep|rest|wind.?down|bedtime)\b", re.I)

    child_events: dict[str, int] = Counter()
    elder_hits: Counter[str] = Counter()
    work_hours: list[int] = []
    health_n = 0
    learn_n = 0
    sleep_n = 0
    early_n = 0
    late_n = 0

    for ev in events:
        summary = " ".join(((ev.get("summary") or "")).split()).strip()
        if not summary or summary == "(no title)":
            continue
        hour = _event_hour(ev.get("start"))
        if hour is not None:
            if hour < 7:
                early_n += 1
            if hour >= 20:
                late_n += 1

        m = name_re.search(summary)
        person = m.group(1) if m else None
        title = summary[: m.start()].strip(" —-–") if m else summary

        if person and (kid_cue.search(title) or kid_cue.search(summary)):
            child_events[person] += 1
        elif person and elder_re.search(person):
            elder_hits[person] += 1
        elif elder_re.search(summary):
            label = elder_re.search(summary)
            if label:
                elder_hits[label.group(1).title()] += 1

        if work_re.search(summary):
            if hour is not None and 7 <= hour <= 18:
                work_hours.append(hour)
            elif hour is None:
                work_hours.append(9)  # count presence even without time
        if health_re.search(summary):
            health_n += 1
        if learn_re.search(summary) and not work_re.search(summary):
            learn_n += 1
        if sleep_re.search(summary):
            sleep_n += 1

    facts: list[Fact] = []

    def _add(statement: str, *, salience: float, ftype: FactType = FactType.VALUE_STATEMENT) -> None:
        facts.append(
            Fact(
                user_id=user_id,
                type=ftype,
                statement=statement,
                salience=salience,
                confidence=0.78,
                source_signal_ids=[],
                written_by="agenda_priorities@v1",
            )
        )

    # Family / kids — one priority per child, inferred from routines.
    for name, n in child_events.most_common(3):
        if n < 2:
            continue
        _add(
            f"Family time with {name} — protecting school, sports, and their day-to-day",
            salience=0.92,
            ftype=FactType.RELATIONSHIP,
        )

    for name, n in elder_hits.most_common(2):
        if n < 1:
            continue
        _add(
            f"Staying close with {name} — visits and check-ins matter",
            salience=0.84,
            ftype=FactType.RELATIONSHIP,
        )

    # Work block inference from timed work-ish events.
    if len(work_hours) >= 3:
        start_h = min(work_hours)
        end_h = max(work_hours) + 1
        # Sensible defaults if the spread is weird.
        if end_h - start_h < 3:
            start_h, end_h = 9, 17
        start_h = max(7, min(start_h, 11))
        end_h = max(start_h + 4, min(end_h, 19))
        _add(
            f"Work focus on weekdays — roughly {_fmt_hour(start_h)}–{_fmt_hour(end_h)} stays protected",
            salience=0.88,
            ftype=FactType.COMMITMENT,
        )
    elif len(work_hours) >= 1:
        _add(
            "Protecting focused work time during the week",
            salience=0.75,
            ftype=FactType.COMMITMENT,
        )

    if health_n >= 2:
        _add(
            "Health and recovery — appointments and mental-health care stay on the calendar",
            salience=0.8,
        )
    elif health_n == 1:
        _add(
            "Making space for health and recovery when it comes up",
            salience=0.7,
        )

    if learn_n >= 2:
        _add(
            "Learning and training — classes that keep you growing outside work",
            salience=0.72,
        )

    if sleep_n >= 1 or (early_n >= 2 and late_n >= 2):
        _add(
            "Sleep and mental recovery — the week shouldn’t run you into the ground",
            salience=0.78,
        )

    # Deduplicate near-identical priorities.
    seen: set[str] = set()
    unique: list[Fact] = []
    for fact in facts:
        key = fact.statement.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(fact)
    return unique[:7]


async def refresh_profile_and_manifesto(
    *,
    user_id: str,
    facts: list[Fact],
    previous_manifesto: Manifesto | None,
) -> tuple[ProfileSnapshot, Manifesto]:
    snapshot = synthesize_snapshot(facts, user_id=user_id)
    statement, source_ids = build_manifesto_statement(facts)
    version = (previous_manifesto.version + 1) if previous_manifesto else 1
    manifesto = Manifesto(
        user_id=user_id,
        version=version,
        statement=statement,
        source_fact_ids=source_ids,
        change_summary="Refreshed from Memory Bank after ingest.",
        updated_at=datetime.now(tz=timezone.utc),
    )
    return snapshot, manifesto


__all__ = [
    "agenda_life_facts",
    "build_manifesto_statement",
    "calendar_pattern_facts",
    "detect_contradictions",
    "infer_priority_facts",
    "refresh_profile_and_manifesto",
    "synthesize_snapshot",
]
