"""Build durable Care Profile + manifesto + contradictions from facts."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from level_core.schemas.bias import Manifesto
from level_core.schemas.care import (
    CARE_ROLE_COLORS,
    CARE_ROLE_LABELS,
    CARE_YOU_COLOR,
    CareGraph,
    CareGraphCategory,
    CareGraphEdge,
    CareGraphNode,
    CareProfile,
    CareRoleId,
    CareRoleState,
    ProtectedWindow,
    active_care_roles,
)
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
            "Still learning which caregiver roles you hold when the week gets hard.",
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
        "As a caregiver, the roles you hold look like:",
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
    ]
    priorities.sort(key=lambda f: f.salience, reverse=True)
    if not priorities:
        return (
            "Still learning which caregiver roles you hold when the week gets hard.",
            [],
        )
    top = priorities[:3]
    labels = [f.statement.rstrip(".") for f in top]
    lines = [
        "As a caregiver, the roles you hold look like:",
        *[f"• {label}" for label in labels],
    ]
    return "\n".join(lines)[:4000], [f.fact_id for f in top]


def care_profile_to_snapshot(profile: CareProfile, *, fact_count: int = 0) -> ProfileSnapshot:
    """Project Care Profile into Priorities UI bullets (role-grouped)."""
    # Detect a shared dump pasted onto every role (ChatGPT Memory care_note).
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
    """Backward-compatible wrapper — prefer :func:`infer_care_profile`."""
    events = [{"summary": s, "start": None} for s in event_summaries]
    return infer_priority_facts(events, user_id=user_id)


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


def _merge_role_feedback(
    inferred: CareRoleState,
    previous: CareProfile | None,
) -> CareRoleState:
    """Preserve Keep / Not me status across re-inference."""
    if previous is None:
        return inferred
    for old in previous.roles:
        if old.role_id is not inferred.role_id:
            continue
        if old.status is BulletStatus.REJECTED:
            return inferred.model_copy(
                update={"status": BulletStatus.REJECTED, "salience": min(inferred.salience, 0.25)}
            )
        if old.status in {BulletStatus.ACCEPTED, BulletStatus.EDITED}:
            return inferred.model_copy(
                update={
                    "status": old.status,
                    "salience": max(inferred.salience, min(0.98, old.salience + 0.05)),
                    "label": old.label if old.status is BulletStatus.EDITED else inferred.label,
                }
            )
    return inferred


def apply_bullet_feedback_to_care_profile(
    profile: CareProfile,
    *,
    bullet_id: str,
    status: BulletStatus,
    text: str | None,
    snapshot: ProfileSnapshot,
) -> CareProfile:
    """Mutate Care Profile from Priorities Keep / Not me / edit."""
    bullet = next((b for b in snapshot.bullets if b.bullet_id == bullet_id), None)
    role_key = bullet.care_role_id if bullet else None
    roles: list[CareRoleState] = []
    for role in profile.roles:
        if role_key and role.role_id.value == role_key:
            sal = role.salience
            if status is BulletStatus.ACCEPTED:
                sal = min(0.98, max(sal, 0.85) + 0.05)
            elif status is BulletStatus.REJECTED:
                sal = min(sal, 0.2)
            elif status is BulletStatus.EDITED and text:
                roles.append(
                    role.model_copy(
                        update={
                            "status": status,
                            "salience": sal,
                            "evidence_summaries": [text[:200], *role.evidence_summaries][:4],
                            "label": role.label,
                        }
                    )
                )
                continue
            roles.append(role.model_copy(update={"status": status, "salience": sal}))
        else:
            roles.append(role)
    return profile.model_copy(
        update={"roles": roles, "version": profile.version + 1, "updated_at": datetime.now(tz=timezone.utc)}
    )


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
        return profile
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
        return profile
    return profile.model_copy(
        update={
            "roles": list(by_id.values()),
            "version": profile.version + 1,
            "updated_at": datetime.now(tz=timezone.utc),
        }
    )


def classify_calendar_event(summary: str) -> CareRoleId | None:
    """Offline fallback classifier when AI ``calendar_role_by_summary`` is empty.

    Prefer holistic Gemini inference (:mod:`care_infer_llm`). This regex path
    only keeps the care graph populated until background AI catches up — it
    will never cover every real calendar title.
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


def agenda_fingerprint(events: list[dict[str, str | None]] | None) -> str:
    """Stable short hash of calendar titles — used to invalidate graph cache."""
    import hashlib

    if not events:
        return "empty"
    titles: list[str] = []
    for ev in events:
        s = re.sub(r"\s+", " ", (ev.get("summary") or "").strip().lower())
        if s:
            titles.append(s)
    titles = sorted(set(titles))[:80]
    digest = hashlib.sha1("|".join(titles).encode("utf-8")).hexdigest()
    return digest[:16]


# Process-local CareGraph cache: user_id → (cache_key, graph).
# Avoids rebuilding the projection on every Profile/Today GET when nothing changed.
_GRAPH_CACHE: dict[str, tuple[str, CareGraph]] = {}


def cached_care_graph(
    profile: CareProfile | None,
    events: list[dict[str, str | None]] | None = None,
) -> tuple[CareGraph | None, CareProfile | None, bool]:
    """Return (graph, profile, dirty).

    ``dirty`` is always False for the process cache (nothing to persist).
    Cache key = care version + agenda fingerprint.
    """
    if profile is None:
        return None, None, False
    key = f"v{profile.version}:{agenda_fingerprint(events)}"
    hit = _GRAPH_CACHE.get(profile.user_id)
    if hit is not None and hit[0] == key:
        return hit[1], profile, False
    graph = build_care_graph(profile, events=events)
    if graph is not None:
        _GRAPH_CACHE[profile.user_id] = (key, graph)
    else:
        _GRAPH_CACHE.pop(profile.user_id, None)
    return graph, profile, False


def invalidate_care_graph_cache(user_id: str) -> None:
    _GRAPH_CACHE.pop(user_id, None)


def resolve_event_care_role(
    summary: str,
    *,
    role_by_summary: dict[str, str] | None = None,
    allow_heuristic_fallback: bool = False,
) -> CareRoleId | None:
    """Prefer AI calendar hints; optionally classify titles the catalog missed."""
    text = summary or ""
    hints = role_by_summary or {}
    if hints:
        key = re.sub(r"\s+", " ", text.strip().lower())
        raw = hints.get(key)
        if raw:
            try:
                return CareRoleId(raw)
            except ValueError:
                pass
        if not allow_heuristic_fallback:
            return None
        return classify_calendar_event(text)
    return classify_calendar_event(text)


def group_events_by_care_role(
    events: list[dict[str, str | None]] | None,
    *,
    role_by_summary: dict[str, str] | None = None,
) -> dict[CareRoleId, int]:
    """Count calendar events per care-role category.

    Prefer AI ``role_by_summary`` hints. Titles missing from the catalog still
    get a coarse offline classify so the graph/load bar aren't skewed to only
    the few titles Gemini has tagged so far.
    """
    counts: Counter[CareRoleId] = Counter()
    if not events:
        return {}
    for ev in events:
        role = resolve_event_care_role(
            ev.get("summary") or "",
            role_by_summary=role_by_summary,
            allow_heuristic_fallback=True,
        )
        if role is not None:
            counts[role] += 1
    return dict(counts)


def _event_duration_minutes(ev: dict[str, str | None | bool]) -> float:
    """Best-effort duration; all-day ≈ half day; missing end ≈ 60m."""
    if ev.get("all_day"):
        return 4.0 * 60.0
    start_raw = ev.get("start")
    end_raw = ev.get("end")
    if not isinstance(start_raw, str) or not start_raw:
        return 60.0
    try:
        from level_core.ingest.google_live import _parse_when

        start = _parse_when(start_raw)
        end = _parse_when(end_raw) if isinstance(end_raw, str) and end_raw else None
    except Exception:  # noqa: BLE001
        return 60.0
    if start is None:
        return 60.0
    if end is None:
        return 60.0
    mins = (end - start).total_seconds() / 60.0
    if mins <= 0:
        return 60.0
    return float(min(mins, 12 * 60))


def filter_events_for_local_week(
    events: list[dict[str, str | None | bool]] | None,
    *,
    timezone_name: str = "America/Los_Angeles",
    now: datetime | None = None,
) -> list[dict[str, str | None | bool]]:
    """Keep events whose start falls in the local Mon–Sun week containing ``now``."""
    from zoneinfo import ZoneInfo

    from level_core.ingest.google_live import _parse_when

    if not events:
        return []
    local_now = (now or datetime.now(tz=timezone.utc)).astimezone(ZoneInfo(timezone_name))
    week_start = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=7)
    out: list[dict[str, str | None | bool]] = []
    for ev in events:
        start_raw = ev.get("start")
        if not isinstance(start_raw, str) or not start_raw:
            continue
        when = _parse_when(start_raw)
        if when is None:
            continue
        local_when = when.astimezone(ZoneInfo(timezone_name))
        if week_start <= local_when < week_end:
            out.append(ev)
    return out


def build_week_role_load(
    profile: CareProfile | None,
    events: list[dict[str, str | None | bool]] | None,
    *,
    timezone_name: str = "America/Los_Angeles",
    now: datetime | None = None,
) -> list[dict[str, float | int | str]]:
    """Stacked load share for the current week — composition, not a balance target.

    Returns rows: role_id, label, color, percent, event_count, minutes.
    """
    week = filter_events_for_local_week(
        events, timezone_name=timezone_name, now=now
    )
    hints = dict(profile.calendar_role_by_summary) if profile else {}
    minutes: Counter[CareRoleId] = Counter()
    counts: Counter[CareRoleId] = Counter()
    for ev in week:
        summary = str(ev.get("summary") or "")
        # AI hint when present; classify uncatalogued titles so partial catalogs
        # don't make one role look like 100% of the week.
        role = resolve_event_care_role(
            summary,
            role_by_summary=hints or None,
            allow_heuristic_fallback=True,
        )
        if role is None:
            continue
        minutes[role] += _event_duration_minutes(ev)
        counts[role] += 1
    total = sum(minutes.values())
    if total <= 0:
        return []
    rows: list[dict[str, float | int | str]] = []
    for role, mins in sorted(minutes.items(), key=lambda kv: (-kv[1], kv[0].value)):
        pct = round(100.0 * mins / total)
        if pct < 1 and counts[role] > 0:
            pct = 1
        rows.append(
            {
                "role_id": role.value,
                "label": CARE_ROLE_LABELS[role],
                "color": CARE_ROLE_COLORS[role],
                "percent": int(pct),
                "event_count": int(counts[role]),
                "minutes": int(round(mins)),
            }
        )
    # Fix rounding so percents sum to ~100.
    if rows:
        drift = 100 - sum(int(r["percent"]) for r in rows)
        if drift != 0:
            rows[0]["percent"] = int(rows[0]["percent"]) + drift
    return rows


def build_holding_summary(
    profile: CareProfile | None,
) -> list[dict[str, str]]:
    """People + load-bearing roles for a role-led Today header."""
    roles = active_care_roles(profile)
    if not roles:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    person_roles = {
        CareRoleId.CHILD_CARE,
        CareRoleId.ELDER_CARE,
        CareRoleId.PARTNER_COPARENT,
    }
    for role in sorted(roles, key=lambda r: r.salience, reverse=True):
        color = CARE_ROLE_COLORS[role.role_id]
        if role.role_id in person_roles and role.people:
            for person in role.people[:3]:
                key = person.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "label": person.strip(),
                        "role_id": role.role_id.value,
                        "color": color,
                    }
                )
        else:
            label = CARE_ROLE_LABELS[role.role_id]
            # Shorter chip for paid work / logistics.
            short = {
                CareRoleId.PAID_WORK: "Work/Job",
                CareRoleId.SELF_RECOVERY: "Self & recovery",
                CareRoleId.HOUSEHOLD_LOGISTICS: "Household",
            }.get(role.role_id, label)
            key = short.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "label": short,
                    "role_id": role.role_id.value,
                    "color": color,
                }
            )
        if len(out) >= 6:
            break
    return out


def build_care_graph(
    profile: CareProfile | None,
    events: list[dict[str, str | None]] | None = None,
) -> CareGraph | None:
    """Star graph: You → people/roles; colored by care role; sized by calendar counts."""
    # Prefer AI people assignment already on the profile; still enforce exclusivity.
    if profile is not None:
        from level_core.profile.care_infer_llm import reconcile_exclusive_people

        profile = reconcile_exclusive_people(profile)

    roles = active_care_roles(profile)
    hints = dict(profile.calendar_role_by_summary) if profile else {}
    event_counts = group_events_by_care_role(
        events,
        role_by_summary=hints or None,
    )
    rejected = {
        r.role_id
        for r in (profile.roles if profile else [])
        if r.status is BulletStatus.REJECTED
    }
    # Never revive a role the user explicitly rejected (e.g. "no co-parent").
    event_counts = {rid: n for rid, n in event_counts.items() if rid not in rejected}
    if not roles and not event_counts:
        return None

    # Ensure roles exist for categories that only appear on the calendar.
    by_id = {r.role_id: r for r in roles}
    for role_id, n in event_counts.items():
        if role_id in rejected:
            continue
        if role_id not in by_id and n > 0:
            by_id[role_id] = CareRoleState(
                role_id=role_id,
                label=CARE_ROLE_LABELS[role_id],
                salience=0.55,
                weekly_load_hours=float(min(20.0, n * 1.25)),
                status=BulletStatus.PENDING,
            )
    roles = list(by_id.values())
    if not roles:
        return None

    center = CareGraphNode(
        id="you",
        label="You",
        kind="you",
        color=CARE_YOU_COLOR,
        role_id=None,
    )
    nodes: list[CareGraphNode] = []
    edges: list[CareGraphEdge] = []
    seen: set[str] = set()
    child_node_ids: list[str] = []

    def _color(role_id: CareRoleId) -> str:
        return CARE_ROLE_COLORS[role_id]

    def _add(
        node: CareGraphNode,
        *,
        from_id: str,
        relation: str,
        role_id: CareRoleId,
    ) -> None:
        if node.id not in seen:
            nodes.append(node)
            seen.add(node.id)
        edges.append(
            CareGraphEdge(
                from_id=from_id,
                to_id=node.id,
                relation=relation,
                role_id=role_id.value,
                color=_color(role_id),
            )
        )

    for role in sorted(roles, key=lambda r: r.salience, reverse=True):
        count = event_counts.get(role.role_id, 0)
        color = _color(role.role_id)
        if role.role_id is CareRoleId.CHILD_CARE:
            if role.people:
                for person in role.people[:3]:
                    nid = f"child-{person.lower()}"
                    _add(
                        CareGraphNode(
                            id=nid,
                            label=person,
                            kind="child",
                            role_id=role.role_id.value,
                            color=color,
                            event_count=count,
                        ),
                        from_id="you",
                        relation="holds",
                        role_id=role.role_id,
                    )
                    child_node_ids.append(nid)
            else:
                nid = "role-child_care"
                _add(
                    CareGraphNode(
                        id=nid,
                        label="Child care",
                        kind="child",
                        role_id=role.role_id.value,
                        color=color,
                        event_count=count,
                    ),
                    from_id="you",
                    relation="holds",
                    role_id=role.role_id,
                )
                child_node_ids.append(nid)
        elif role.role_id is CareRoleId.ELDER_CARE:
            labels = role.people[:2] or ["Elder care"]
            for person in labels:
                nid = f"elder-{person.lower().replace(' ', '-')}"
                _add(
                    CareGraphNode(
                        id=nid,
                        label=person if role.people else role.label,
                        kind="elder",
                        role_id=role.role_id.value,
                        color=color,
                        event_count=count,
                    ),
                    from_id="you",
                    relation="holds",
                    role_id=role.role_id,
                )
        elif role.role_id is CareRoleId.PAID_WORK:
            _add(
                CareGraphNode(
                    id="role-paid_work",
                    label="Work",
                    kind="work",
                    role_id=role.role_id.value,
                    color=color,
                    event_count=count,
                ),
                from_id="you",
                relation="carries",
                role_id=role.role_id,
            )
        elif role.role_id is CareRoleId.SELF_RECOVERY:
            _add(
                CareGraphNode(
                    id="role-self_recovery",
                    label="Recovery",
                    kind="recovery",
                    role_id=role.role_id.value,
                    color=color,
                    event_count=count,
                ),
                from_id="you",
                relation="holds",
                role_id=role.role_id,
            )
        elif role.role_id is CareRoleId.HOUSEHOLD_LOGISTICS:
            _add(
                CareGraphNode(
                    id="role-household_logistics",
                    label="Logistics",
                    kind="logistics",
                    role_id=role.role_id.value,
                    color=color,
                    event_count=count,
                ),
                from_id="you",
                relation="carries",
                role_id=role.role_id,
            )
        elif role.role_id is CareRoleId.PARTNER_COPARENT:
            label = role.people[0] if role.people else "Co-parent"
            nid = f"helper-{label.lower().replace(' ', '-')}"
            _add(
                CareGraphNode(
                    id=nid,
                    label=label,
                    kind="helper",
                    hint="May share child-care load",
                    role_id=role.role_id.value,
                    color=color,
                    event_count=count,
                ),
                from_id="you",
                relation="coordinates",
                role_id=role.role_id,
            )
            help_color = CARE_ROLE_COLORS[CareRoleId.CHILD_CARE]
            for cid in child_node_ids[:3]:
                edges.append(
                    CareGraphEdge(
                        from_id=nid,
                        to_id=cid,
                        relation="can_help",
                        role_id=CareRoleId.CHILD_CARE.value,
                        color=help_color,
                    )
                )

    # Occasional helpers (friends/neighbors): arrow points at who they help — not You→holds.
    if profile is not None:
        help_color = CARE_ROLE_COLORS[CareRoleId.CHILD_CARE]
        helper_color = CARE_ROLE_COLORS[CareRoleId.PARTNER_COPARENT]
        for helper in profile.helpers[:4]:
            label = (helper.name or "").strip() or "Friend"
            nid = f"helper-{label.lower().replace(' ', '-')}"
            if nid not in seen:
                nodes.append(
                    CareGraphNode(
                        id=nid,
                        label=label,
                        kind="helper",
                        hint=helper.hint or "Occasionally helps with care",
                        role_id=None,
                        color=helper_color,
                        event_count=0,
                    )
                )
                seen.add(nid)
            targets: list[str] = []
            for person in helper.helps[:3]:
                key = person.lower()
                for cid in child_node_ids:
                    if cid == f"child-{key}" or cid.endswith(f"-{key}"):
                        targets.append(cid)
                # Elder targets
                eid = f"elder-{key.replace(' ', '-')}"
                if eid in seen:
                    targets.append(eid)
            if not targets and child_node_ids:
                targets = child_node_ids[:1]
            for tid in targets:
                edges.append(
                    CareGraphEdge(
                        from_id=nid,
                        to_id=tid,
                        relation="can_help",
                        role_id=CareRoleId.CHILD_CARE.value,
                        color=help_color,
                    )
                )

    edge_keys: set[tuple[str, str, str]] = set()
    unique_edges: list[CareGraphEdge] = []
    for e in edges:
        key = (e.from_id, e.to_id, e.relation)
        if key in edge_keys:
            continue
        edge_keys.add(key)
        unique_edges.append(e)

    categories: list[CareGraphCategory] = []
    for role_id, n in sorted(event_counts.items(), key=lambda kv: (-kv[1], kv[0].value)):
        categories.append(
            CareGraphCategory(
                role_id=role_id.value,
                label=CARE_ROLE_LABELS[role_id],
                color=CARE_ROLE_COLORS[role_id],
                event_count=n,
            )
        )
    # Include active roles with zero calendar hits so the legend still explains colors.
    for role in roles:
        if role.role_id not in event_counts:
            categories.append(
                CareGraphCategory(
                    role_id=role.role_id.value,
                    label=CARE_ROLE_LABELS[role.role_id],
                    color=CARE_ROLE_COLORS[role.role_id],
                    event_count=0,
                )
            )

    return CareGraph(
        center=center,
        nodes=nodes,
        edges=unique_edges,
        categories=categories,
    )


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
    """Light upsert when the user tells Level more about their care load."""
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
    return profile.model_copy(
        update={
            "roles": list(by_id.values()),
            "version": profile.version + 1,
            "updated_at": datetime.now(tz=timezone.utc),
        }
    )


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
    late_work = 0
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
                    if hour >= 16:
                        late_work += 1
                else:
                    work_hours.append(19)
        elif work_re.search(summary):
            if hour is not None and 7 <= hour <= 18:
                work_hours.append(hour)
            elif hour is None:
                work_hours.append(9)
            if hour is not None and hour >= 16:
                late_work += 1
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
        roles.append(_merge_role_feedback(role, previous))

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
        roles.append(_merge_role_feedback(role, previous))
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
        roles.append(_merge_role_feedback(role, previous))

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
        roles.append(_merge_role_feedback(role, previous))

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
        roles.append(_merge_role_feedback(role, previous))

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
        roles.append(_merge_role_feedback(role, previous))

    conflicts: list[str] = []
    role_ids = {r.role_id for r in roles}
    if CareRoleId.CHILD_CARE in role_ids and CareRoleId.PAID_WORK in role_ids and late_work >= 2:
        conflicts.append(
            "Work/Job is leaning into late blocks that can crowd out child care pickups."
        )
    if CareRoleId.CHILD_CARE in role_ids and CareRoleId.SELF_RECOVERY in role_ids and late_n >= 3:
        conflicts.append(
            "Late evenings pile onto child care — self & recovery is the first role to erode."
        )

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
        conflict_summaries=conflicts,
    )
    return profile, unique[:8]


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
        if role.people:
            role_bits.append(f"{role.label.lower()} for {', '.join(role.people[:2])}")
        else:
            role_bits.append(role.label.lower())
    if role_bits:
        if len(role_bits) == 1:
            sentences.append(f"You're holding {role_bits[0]}.")
        elif len(role_bits) == 2:
            sentences.append(f"You're holding {role_bits[0]} and {role_bits[1]}.")
        else:
            sentences.append(
                "You're holding "
                + ", ".join(role_bits[:-1])
                + f", and {role_bits[-1]}."
            )

    helpers = list(care_profile.helpers) if care_profile else []
    if helpers:
        names = [h.name for h in helpers[:3] if h.name]
        if len(names) == 1:
            sentences.append(f"{names[0]} sometimes helps with care load.")
        elif names:
            sentences.append(f"{', '.join(names)} sometimes help with care load.")

    # Only surface personality / style / interests when facts explicitly support them.
    style_hits: list[str] = []
    interest_hits: list[str] = []
    personality_hits: list[str] = []
    for fact in facts:
        stmt = (fact.statement or "").strip()
        if len(stmt) < 12:
            continue
        low = stmt.lower()
        # Communication / answer style
        if any(
            k in low
            for k in (
                "concise",
                "short answer",
                "brief",
                "spoken",
                "while driving",
                "in the car",
                "audio",
                "no lecture",
                "don't lecture",
                "direct answer",
                "prefers concise",
                "prefer concise",
                "prefer short",
                "prefers short",
            )
        ):
            if "spoken" in low or "car" in low or "audio" in low or "driving" in low:
                style_hits.append("short spoken updates")
            elif "lecture" in low:
                style_hits.append("concrete tradeoffs over lectures")
            else:
                style_hits.append("concise answers")
        # Interests (require like/enjoy/interest wording — don't invent from calendar alone)
        if fact.type is FactType.PREFERENCE or any(
            k in low for k in (" likes ", " like ", "enjoys", "interest", "into cooking", "loves ")
        ):
            if any(k in low for k in ("cook", "cooking", "meal")):
                interest_hits.append("cooking")
            if any(k in low for k in ("walk", "hiking", "run")):
                interest_hits.append("walks")
            if any(k in low for k in ("soccer",)):
                interest_hits.append("Jordan's soccer" if "jordan" in low else "soccer")
        # Personality / temperament — only explicit language
        if any(
            k in low
            for k in (
                "anxious",
                "overwhelmed",
                "practical",
                "direct",
                "guilt",
                "over-accommodate",
                "overaccommodate",
            )
        ):
            if "anxious" in low or "overwhelm" in low:
                personality_hits.append("gets stretched when calendars collide")
            if "practical" in low or "direct" in low:
                personality_hits.append("practical and direct")
            if "over-accommodate" in low or "overaccommodate" in low or "accommodate" in low:
                personality_hits.append("tends to accommodate work before protecting family time")

    def _uniq(items: list[str], *, limit: int = 2) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= limit:
                break
        return out

    styles = _uniq(style_hits)
    if styles:
        sentences.append(f"Prefers {styles[0]}" + (f" and {styles[1]}" if len(styles) > 1 else "") + ".")

    interests = _uniq(interest_hits)
    if interests:
        if len(interests) == 1:
            sentences.append(f"Outside the grind: {interests[0]}.")
        else:
            sentences.append(f"Outside the grind: {' and '.join(interests)}.")

    traits = _uniq(personality_hits, limit=2)
    if traits:
        sentences.append(traits[0].capitalize() + (f"; {traits[1]}" if len(traits) > 1 else "") + ".")

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
    "people_from_note",
    "people_mentions_from_facts",
    "refresh_profile_and_manifesto",
    "resolve_event_care_role",
    "synthesize_snapshot",
]
