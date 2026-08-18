"""Build durable Care Profile + manifesto + contradictions from facts."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from level_core.schemas.bias import Manifesto
from level_core.schemas.care import (
    CARE_ROLE_LABELS,
    CareProfile,
    CareRoleId,
    active_care_roles,
    clean_conflict_summaries,
)
from level_core.schemas.profile import (
    BulletCategory,
    BulletStatus,
    Contradiction,
    ProfileBullet,
    ProfileSnapshot,
)
from level_core.schemas.signal import Fact, FactType



from level_core.profile.care_feedback import (
    apply_bullet_feedback_to_care_profile,
    merge_role_feedback,
)
from level_core.profile.care_graph import (
    agenda_fingerprint,
    build_care_graph,
    build_holding_summary,
    build_week_role_load,
    cached_care_graph,
    filter_events_for_local_week,
    group_events_by_care_role,
    invalidate_care_graph_cache,
    resolve_event_care_role,
)
from level_core.profile.care_heuristic import (
    adjust_care_profile_from_note,
    classify_calendar_event,
    extract_people_from_calendar_title,
    infer_care_profile,
    infer_care_profile_heuristic,
    merge_people_into_care_profile,
    people_from_note,
    people_mentions_from_facts,
)


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


def _is_day_event_statement(text: str) -> bool:
    """True for ephemeral check-in notes — not who the caregiver is."""
    stmt = " ".join((text or "").split()).strip().lower()
    return stmt.startswith("i notice:") or stmt.startswith("i notice ")


def build_manifesto_from_care_profile(profile: CareProfile) -> tuple[str, list[str]]:
    """Role-ordered manifesto — deterministic synthesis from Care Profile."""
    roles = [
        r
        for r in profile.roles
        if r.status is not BulletStatus.REJECTED and r.salience >= 0.4
    ]
    roles.sort(key=lambda r: r.salience, reverse=True)
    if not roles:
        return (
            "Still learning which caregiver roles I provide when the week gets hard.",
            [],
        )
    top = roles[:4]
    labels: list[str] = []
    source_ids: list[str] = []
    for role in top:
        people = f" ({', '.join(role.people)})" if role.people else ""
        labels.append(f"{role.label}{people}")
        source_ids.extend(role.source_fact_ids[:2])
    lines = [
        "As a caregiver, my roles look like:",
        *[f"• {label}" for label in labels],
    ]
    if profile.conflict_summaries:
        lines.append("Watch for care collisions when these overlap.")
    return "\n".join(lines)[:4000], source_ids[:12]


def build_manifesto_statement(facts: list[Fact]) -> tuple[str, list[str]]:
    """Fallback manifesto from facts when no Care Profile exists yet."""
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
        and not _is_day_event_statement(f.statement)
    ]
    priorities.sort(key=lambda f: f.salience, reverse=True)
    if not priorities:
        return (
            "Still learning which caregiver roles I provide when the week gets hard.",
            [],
        )
    top = priorities[:3]
    labels = [f.statement.rstrip(".") for f in top]
    lines = [
        "As a caregiver, my roles look like:",
        *[f"• {label}" for label in labels],
    ]
    return "\n".join(lines)[:4000], [f.fact_id for f in top]


def care_profile_to_snapshot(profile: CareProfile, *, fact_count: int = 0) -> ProfileSnapshot:
    """Project Care Profile into Priorities UI bullets (role-grouped)."""
    # Detect a shared dump pasted onto every role.
    evidence_heads = [
        (r.evidence_summaries[0][:80].strip().lower() if r.evidence_summaries else "")
        for r in profile.roles
        if r.status is not BulletStatus.REJECTED
    ]
    shared_dump = False
    if len(evidence_heads) >= 2:
        non_empty = [e for e in evidence_heads if e]
        if non_empty and len(set(non_empty)) == 1 and len(non_empty[0]) >= 40:
            shared_dump = True

    bullets: list[ProfileBullet] = []
    for role in sorted(profile.roles, key=lambda r: r.salience, reverse=True):
        if role.status is BulletStatus.REJECTED:
            continue
        people = f" with {', '.join(role.people)}" if role.people else ""
        window_bit = ""
        if role.protected_windows:
            window_bit = f" — {role.protected_windows[0].label}"
        structured = f"{role.label}{people}{window_bit}".strip()
        ev = (role.evidence_summaries[0] if role.evidence_summaries else "").strip()
        # Prefer role-specific structure. Never clone one long Memory summary onto every bullet.
        if people or window_bit:
            text = structured
        elif ev and not shared_dump and len(ev) <= 140 and ev.count(".") <= 1:
            text = f"{role.label} — {ev}"
        else:
            text = structured or role.label
        bullets.append(
            ProfileBullet(
                category=BulletCategory.ROLE,
                text=text[:220],
                status=role.status,
                source_fact_ids=list(role.source_fact_ids),
                care_role_id=role.role_id.value,
            )
        )
    contradictions: list[Contradiction] = []
    for i, summary in enumerate(profile.conflict_summaries[:4]):
        contradictions.append(
            Contradiction(
                user_id=profile.user_id,
                topic=f"role_conflict_{i}",
                fact_id_a=bullets[0].source_fact_ids[0] if bullets and bullets[0].source_fact_ids else "none",
                fact_id_b=bullets[1].source_fact_ids[0]
                if len(bullets) > 1 and bullets[1].source_fact_ids
                else "none",
                summary=summary[:400],
            )
        )
    return ProfileSnapshot(
        user_id=profile.user_id,
        bullets=bullets[:8],
        contradictions=contradictions,
        needs_review=any(b.status is BulletStatus.PENDING for b in bullets),
        fact_count=fact_count,
    )


def synthesize_snapshot(facts: list[Fact], *, user_id: str) -> ProfileSnapshot:
    """Build a short list of inferred priorities the user can Keep / Not me."""
    bullets: list[ProfileBullet] = []
    ranked = sorted(
        [
            f
            for f in facts
            if f.confidence >= 0.55
            and not _is_analytics_statement(f.statement)
            and not _is_day_event_statement(f.statement)
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
    """Backward-compatible wrapper — prefer :func:`infer_care_profile`."""
    events = [{"summary": s, "start": None} for s in event_summaries]
    return infer_priority_facts(events, user_id=user_id)


def infer_priority_facts(
    events: list[dict[str, str | None]],
    *,
    user_id: str,
) -> list[Fact]:
    """Backward-compatible: facts only from :func:`infer_care_profile`."""
    _profile, facts = infer_care_profile(events, user_id=user_id)
    return facts


async def refresh_profile_and_manifesto(
    *,
    user_id: str,
    facts: list[Fact],
    previous_manifesto: Manifesto | None,
    care_profile: CareProfile | None = None,
) -> tuple[ProfileSnapshot, Manifesto, CareProfile | None]:
    if care_profile and care_profile.roles:
        snapshot = care_profile_to_snapshot(care_profile, fact_count=len(facts))
        # Merge non-role facts into snapshot if thin
        if len(snapshot.bullets) < 2:
            fallback = synthesize_snapshot(facts, user_id=user_id)
            snapshot = snapshot.model_copy(
                update={"bullets": snapshot.bullets + fallback.bullets}
            )
            snapshot = snapshot.model_copy(update={"bullets": snapshot.bullets[:8]})
        statement, source_ids = build_manifesto_from_care_profile(care_profile)
        change = "Refreshed Care Profile from caregiver role inference."
    else:
        snapshot = synthesize_snapshot(facts, user_id=user_id)
        statement, source_ids = build_manifesto_statement(facts)
        change = "Refreshed from Memory Bank after ingest."
    version = (previous_manifesto.version + 1) if previous_manifesto else 1
    manifesto = Manifesto(
        user_id=user_id,
        version=version,
        statement=statement,
        source_fact_ids=source_ids,
        change_summary=change,
        updated_at=datetime.now(tz=timezone.utc),
    )
    return snapshot, manifesto, care_profile


def build_about_summary(
    *,
    care_profile: CareProfile | None,
    facts: list[Fact],
) -> str | None:
    """Short grounded portrait from care + facts. Omit anything we can't support."""
    sentences: list[str] = []

    roles = active_care_roles(care_profile) if care_profile else []
    role_bits: list[str] = []
    for role in sorted(roles, key=lambda r: r.salience, reverse=True)[:4]:
        people = ", ".join(role.people[:2]) if role.people else ""
        if role.role_id is CareRoleId.CHILD_CARE and people:
            role_bits.append(f"child care for {people}")
        elif role.role_id is CareRoleId.ELDER_CARE and people:
            role_bits.append(f"elder care for {people}")
        elif role.role_id is CareRoleId.PARTNER_COPARENT and people:
            role_bits.append(f"co-parenting with {people}")
        elif role.role_id is CareRoleId.PAID_WORK:
            role_bits.append("work")
        elif role.role_id is CareRoleId.SELF_RECOVERY:
            role_bits.append("my own recovery")
        elif role.people:
            role_bits.append(f"{role.label.lower()} for {people}")
        else:
            role_bits.append(role.label.lower())
    if role_bits:
        # First person + "provide" — concrete care language, not "hold/carry".
        if len(role_bits) == 1:
            sentences.append(f"I provide {role_bits[0]}.")
        elif len(role_bits) == 2:
            sentences.append(f"I provide {role_bits[0]} and {role_bits[1]}.")
        else:
            sentences.append(
                "I provide "
                + ", ".join(role_bits[:-1])
                + f", and {role_bits[-1]}."
            )

    helpers = list(care_profile.helpers) if care_profile else []
    if helpers:
        names = [h.name for h in helpers[:3] if h.name]
        if len(names) == 1:
            sentences.append(f"{names[0]} sometimes helps me with care.")
        elif names:
            sentences.append(f"{', '.join(names)} sometimes help me with care.")

    # Project AI conflict copy + short memory facts as-written — no keyword personality maps.
    conflicts = clean_conflict_summaries(
        care_profile.conflict_summaries if care_profile else None
    )
    if conflicts:
        conflict = conflicts[0]
        if len(conflict) <= 160:
            sentences.append(
                conflict if conflict.endswith((".", "!", "?")) else f"{conflict}."
            )

    fact_bits: list[str] = []
    for fact in facts:
        if fact.type not in {
            FactType.PREFERENCE,
            FactType.CONSTRAINT,
            FactType.VALUE_STATEMENT,
            FactType.CONCERN,
        }:
            continue
        if fact.confidence < 0.6:
            continue
        if _is_analytics_statement(fact.statement) or _is_day_event_statement(fact.statement):
            continue
        stmt = " ".join((fact.statement or "").split()).strip().rstrip(".")
        if not (24 <= len(stmt) <= 110):
            continue
        fact_bits.append(stmt)
        if len(fact_bits) >= 2:
            break
    for bit in fact_bits:
        sentences.append(bit if bit.endswith(("!", "?")) else f"{bit}.")

    if not sentences:
        return None
    # Cap length — portrait, not essay.
    text = " ".join(sentences)
    if len(text) > 420:
        text = text[:417].rsplit(" ", 1)[0] + "…"
    return text


__all__ = [
    "adjust_care_profile_from_note",
    "agenda_life_facts",
    "apply_bullet_feedback_to_care_profile",
    "build_about_summary",
    "build_care_graph",
    "cached_care_graph",
    "agenda_fingerprint",
    "invalidate_care_graph_cache",
    "build_holding_summary",
    "build_week_role_load",
    "build_manifesto_from_care_profile",
    "build_manifesto_statement",
    "calendar_pattern_facts",
    "care_profile_to_snapshot",
    "classify_calendar_event",
    "detect_contradictions",
    "extract_people_from_calendar_title",
    "filter_events_for_local_week",
    "group_events_by_care_role",
    "infer_care_profile",
    "infer_care_profile_heuristic",
    "infer_priority_facts",
    "merge_people_into_care_profile",
    "merge_role_feedback",
    "people_from_note",
    "people_mentions_from_facts",
    "refresh_profile_and_manifesto",
    "resolve_event_care_role",
    "synthesize_snapshot",
]
