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
    FactType.VALUE_STATEMENT: BulletCategory.VALUE,
    FactType.COMMITMENT: BulletCategory.COMMITMENT,
    FactType.CONSTRAINT: BulletCategory.CONSTRAINT,
    FactType.RELATIONSHIP: BulletCategory.RELATIONSHIP,
    FactType.EVENT: BulletCategory.LOAD,
}

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


def build_manifesto_statement(facts: list[Fact]) -> tuple[str, list[str]]:
    """Template manifesto from high-salience values/commitments/constraints."""
    values = [
        f
        for f in facts
        if f.type is FactType.VALUE_STATEMENT and f.confidence >= 0.5
    ]
    commits = [f for f in facts if f.type is FactType.COMMITMENT and f.confidence >= 0.5]
    constraints = [
        f for f in facts if f.type is FactType.CONSTRAINT and f.confidence >= 0.5
    ]
    values.sort(key=lambda f: f.salience, reverse=True)
    commits.sort(key=lambda f: f.salience, reverse=True)
    constraints.sort(key=lambda f: f.salience, reverse=True)

    parts: list[str] = []
    source_ids: list[str] = []
    if values:
        parts.append("I care about: " + "; ".join(f.statement.rstrip(".") for f in values[:3]) + ".")
        source_ids.extend(f.fact_id for f in values[:3])
    if commits:
        parts.append(
            "I have committed to: " + "; ".join(f.statement.rstrip(".") for f in commits[:3]) + "."
        )
        source_ids.extend(f.fact_id for f in commits[:3])
    if constraints:
        parts.append(
            "Hard limits I need to respect: "
            + "; ".join(f.statement.rstrip(".") for f in constraints[:3])
            + "."
        )
        source_ids.extend(f.fact_id for f in constraints[:3])
    if not parts:
        statement = (
            "I am still learning what matters most in my decisions. "
            "I want choices I can defend a week later."
        )
        return statement, []
    statement = " ".join(parts)
    if len(statement) < 20:
        statement = statement + " I want to stay honest about tradeoffs."
    return statement[:4000], source_ids


def synthesize_snapshot(facts: list[Fact], *, user_id: str) -> ProfileSnapshot:
    """Deterministic profile bullets + contradictions from the Memory Bank."""
    bullets: list[ProfileBullet] = []
    # Prefer durable types; skip low-confidence noise.
    ranked = sorted(
        [f for f in facts if f.confidence >= 0.55],
        key=lambda f: (f.salience, f.confidence),
        reverse=True,
    )
    per_cat: dict[BulletCategory, int] = defaultdict(int)
    for fact in ranked:
        cat = _TYPE_TO_CATEGORY.get(fact.type)
        if cat is None:
            continue
        if per_cat[cat] >= 3:
            continue
        per_cat[cat] += 1
        bullets.append(
            ProfileBullet(
                category=cat,
                text=fact.statement[:400],
                source_fact_ids=[fact.fact_id],
            )
        )

    contradictions = detect_contradictions(facts, user_id=user_id)
    for c in contradictions[:4]:
        bullets.append(
            ProfileBullet(
                category=BulletCategory.CONTRADICTION,
                text=c.summary[:400],
                source_fact_ids=[c.fact_id_a, c.fact_id_b],
            )
        )

    return ProfileSnapshot(
        user_id=user_id,
        bullets=bullets[:16],
        contradictions=contradictions,
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
    "build_manifesto_statement",
    "calendar_pattern_facts",
    "detect_contradictions",
    "refresh_profile_and_manifesto",
    "synthesize_snapshot",
]
