"""AI-first Care Profile inference.

Pipeline: calendar / notes / memory snippets → Gemini structured infer →
Care Profile mutation. Regex heuristics live only as offline fallback in
opt-in ``LEVEL_ALLOW_HEURISTIC_CARE`` / tests only
(:func:`synthesize.infer_care_profile_heuristic`). See ``ai_wrappers``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.models.base import GeminiClient, GenerationRequest
from level_core.models.factory import build_gemini_client
from level_core.observability.logger import get_logger
from level_core.schemas.care import (
    CARE_ROLE_LABELS,
    CareHelper,
    CareProfile,
    CareRoleId,
    CareRoleState,
    clean_conflict_summaries,
)
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import Fact, FactType

_logger = get_logger(__name__)

_ALL_ROLES = frozenset(r.value for r in CareRoleId)
_PERSON_ROLES = frozenset(
    {
        CareRoleId.CHILD_CARE.value,
        CareRoleId.ELDER_CARE.value,
        CareRoleId.PARTNER_COPARENT.value,
    }
)

class CarePersonAssign(BaseModel):
    """One human in the care graph — never one row per nickname."""

    name: str = ""
    role: str = "child_care"
    evidence: str = ""
    also_known_as: list[str] = Field(
        default_factory=list,
        description="Nicknames / kinship labels for the same human (Papa, Dad).",
    )
    relationship: str = Field(
        default="",
        description=(
            "How this person relates to the caregiver — short phrase from context "
            "(e.g. parent, child, co-parent, spouse, sibling). Empty if unclear."
        ),
    )


class CarePeopleConsolidate(BaseModel):
    """AI wrapper: collapse nickname duplicates into canonical people."""

    people: list[CarePersonAssign] = Field(default_factory=list)


class CareEventAssign(BaseModel):
    summary: str = ""
    role: str = "other"


class CareRoleInfer(BaseModel):
    role_id: str = ""
    salience: float = 0.7
    weekly_load_hours: float = 4.0
    people: list[str] = Field(default_factory=list)
    evidence: str = ""
    present: bool = True


class CareHolisticInfer(BaseModel):
    """Full care-load model from one Gemini pass."""

    roles: list[CareRoleInfer] = Field(default_factory=list)
    people: list[CarePersonAssign] = Field(default_factory=list)
    events: list[CareEventAssign] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)


class CareHelperAssign(BaseModel):
    name: str = ""
    helps: list[str] = Field(default_factory=list)
    hint: str = "Occasionally helps with care"
    helps_role: str = "child_care"


class CareNoteUpdate(BaseModel):
    """Structured mutation from a Tell Level / profile note."""

    reply: str = ""
    reject_roles: list[str] = Field(default_factory=list)
    accept_roles: list[str] = Field(default_factory=list)
    people: list[CarePersonAssign] = Field(default_factory=list)
    helpers: list[CareHelperAssign] = Field(default_factory=list)
    evidence: str = ""
    conflicts: list[str] = Field(default_factory=list)


class WeekEventClassifyOut(BaseModel):
    """Specialized wrapper: this week's titles → care roles."""

    events: list[CareEventAssign] = Field(default_factory=list)


def _norm_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _unique_titles(events: list[dict[str, str | None]], *, limit: int = 48) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for ev in events:
        raw = (ev.get("summary") or "").strip()
        if not raw or raw == "(no title)":
            continue
        key = _norm_title(raw)
        if key in seen:
            continue
        seen.add(key)
        out.append(raw[:160])
        if len(out) >= limit:
            break
    return out


def _match_title(raw: str, catalog: list[str]) -> str | None:
    key = _norm_title(raw)
    if not key:
        return None
    for title in catalog:
        if _norm_title(title) == key:
            return title
    for title in catalog:
        t = _norm_title(title)
        if key in t or t in key:
            return title
    return None


def _parse_role(raw: str, *, allowed: frozenset[str] | None = None) -> CareRoleId | None:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "child": CareRoleId.CHILD_CARE.value,
        "kids": CareRoleId.CHILD_CARE.value,
        "child_care": CareRoleId.CHILD_CARE.value,
        "elder": CareRoleId.ELDER_CARE.value,
        "elder_care": CareRoleId.ELDER_CARE.value,
        "parent_care": CareRoleId.ELDER_CARE.value,
        "partner": CareRoleId.PARTNER_COPARENT.value,
        "coparent": CareRoleId.PARTNER_COPARENT.value,
        "co_parent": CareRoleId.PARTNER_COPARENT.value,
        "partner_coparent": CareRoleId.PARTNER_COPARENT.value,
        "work": CareRoleId.PAID_WORK.value,
        "paid_work": CareRoleId.PAID_WORK.value,
        "recovery": CareRoleId.SELF_RECOVERY.value,
        "self": CareRoleId.SELF_RECOVERY.value,
        "self_recovery": CareRoleId.SELF_RECOVERY.value,
        "logistics": CareRoleId.HOUSEHOLD_LOGISTICS.value,
        "household": CareRoleId.HOUSEHOLD_LOGISTICS.value,
        "household_logistics": CareRoleId.HOUSEHOLD_LOGISTICS.value,
    }
    key = aliases.get(key, key)
    allowed = allowed or _ALL_ROLES
    if key not in allowed:
        return None
    try:
        return CareRoleId(key)
    except ValueError:
        return None


def _display_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return ""
    low = cleaned.lower()
    if low in {"mom", "dad", "mother", "father", "grandma", "grandpa", "nan", "pop"}:
        return cleaned[:1].upper() + cleaned[1:].lower()
    return cleaned


_SYSTEM_CARE = (
    "You are Level's Care Profile engine. You read a caregiver's calendar and "
    "memory snippets holistically, then infer which care roles they hold and for whom.\n"
    "Use judgment from full context — memory can reclassify ambiguous titles "
    "(e.g. Memory says charger is for work + calendar has \"Meeting\" → paid_work).\n"
    "Roles (use these ids exactly): child_care, elder_care, paid_work, "
    "self_recovery, household_logistics, partner_coparent.\n"
    "Role meanings (critical — do not blur these):\n"
    "- child_care: kids' school, pickup, sports, pediatric care, parenting load.\n"
    "- elder_care: aging parents/relatives — visits, meds, appointments for them.\n"
    "- paid_work: job / career time — meetings, standups, syncs, client calls, "
    "office, shifts, deadlines, professional development. Ambiguous titles like "
    "\"Meeting\", \"Call\", \"Sync\", \"1:1\" are paid_work unless memory clearly "
    "says they are personal.\n"
    "- self_recovery: REST and wellbeing only — sleep, wind-down, therapy/"
    "counseling for the caregiver, restorative exercise. "
    "Classes, courses, night school, lectures, certifications, and skill-building "
    "are obligations (paid_work if career-related, otherwise other) — never "
    "self_recovery.\n"
    "- household_logistics: errands, forms, groceries, admin glue.\n"
    "- partner_coparent: only with real co-parent/partner sharing child care.\n"
    "Rules:\n"
    "- Infer holistically from calendar + memory together; do not keyword-match "
    "naively and do not invent categories from title words alone when memory "
    "clarifies intent.\n"
    "- Each named person belongs to at most ONE role.\n"
    "- ONE human = ONE people[] entry. Prefer a given name when known "
    "(Robert not Papa/Dad). Put nicknames in also_known_as. "
    "Never list Papa, Dad, and Robert as three elder_care people if they are "
    "the same aging parent — that is one person.\n"
    "- Same for kids: Nova and \"N\" are one child if context says so.\n"
    "- For each person, set relationship: a short phrase for how they relate to "
    "the caregiver (parent, child, co-parent, spouse, sibling, etc.) inferred "
    "from calendar + memory — not a fixed vocabulary; leave empty if unclear.\n"
    "- Mom/Dad/Mother/Father/Grandma/Grandpa/Papa refer to elders, never child_care.\n"
    "- Titles like \"Pharmacy pickup — Mom's meds\" or \"dinner drop-off for Mom\" "
    "are elder_care events.\n"
    "- Only include partner_coparent if there is real evidence of a co-parent/"
    "partner sharing child care; solo parents must omit it.\n"
    "- Salience 0..1 reflects how load-bearing the role is.\n"
    "- weekly_load_hours is a rough estimate.\n"
    "- facts: short first-person statements suitable for Memory Bank.\n"
    "- conflicts: ONLY when there is a forward-looking squeeze the caregiver can act on "
    "(what to protect / what not to book over). One plain sentence; name real titles or "
    "people. Prefer an empty list over a history lesson. "
    "Bad: 'The HOLD block indicates work meetings were previously scheduled over pickups, "
    "creating a conflict between paid_work and child_care.' "
    "Good: 'Keep late meetings off the school-run window — pickup is at risk.' "
    "Never use role_id enums, 'indicates', or retrospective archaeology. "
    "Never label classes/courses as self_recovery in a conflict."
)


async def infer_care_holistic(
    gemini: GeminiClient,
    *,
    events: list[dict[str, str | None]],
    fact_snippets: list[str] | None = None,
    previous: CareProfile | None = None,
    model_id: str | None = None,
) -> CareHolisticInfer | None:
    """One structured call: full Care Profile + event categories + facts.

    Retries with fewer titles when Gemini truncates / returns broken JSON —
    busy calendars were overflowing the output budget and leaving Care Profile empty.
    """
    all_titles = _unique_titles(events, limit=48)
    if not all_titles and not (fact_snippets or []):
        return None
    settings = get_settings()
    model = model_id or settings.fast_model
    facts_block = "\n".join(f"- {s}" for s in (fact_snippets or [])[:12]) or "(none)"
    prev_block = "(none)"
    if previous and previous.roles:
        lines = []
        for r in previous.roles:
            st = r.status.value
            peeps = f" people={','.join(r.people)}" if r.people else ""
            lines.append(f"- {r.role_id.value} status={st} salience={r.salience:.2f}{peeps}")
        prev_block = "\n".join(lines)

    # Shrink title count on parse/unavailable failures (MAX_TOKENS truncates JSON).
    for title_cap in (24, 14, 8):
        titles = all_titles[:title_cap]
        titles_block = "\n".join(f"- {t}" for t in titles) or "(no calendar titles)"
        event_cap = min(len(titles), 18)
        prompt = (
            "Infer this caregiver's Care Profile from the data below.\n"
            "Classify from the combination of calendar titles AND memory snippets — "
            "memory can resolve ambiguous titles.\n"
            "Return JSON with:\n"
            "- roles: list of {role_id, salience, weekly_load_hours, people[], evidence, present}\n"
            "  Include a role only when present=true and there is real evidence.\n"
            "- people: ONE entry per human with role in "
            "{child_care, elder_care, partner_coparent}, short evidence, "
            "also_known_as for nicknames, and relationship (how they relate to the "
            "caregiver — e.g. parent, child, co-parent). Prefer given names "
            "(Robert, Nova, Theo). roles.people must use those same canonical "
            "names only — no duplicate nickname rows.\n"
            f"- events: classify at most {event_cap} distinctive titles from the list into "
            "{child_care, elder_care, paid_work, self_recovery, household_logistics, "
            "partner_coparent, other}; copy the exact title string. Prefer named people "
            "and ambiguous titles (Meeting, Pickup, Dr.) over near-duplicates.\n"
            "  Examples: Meeting/Call/Sync/1:1 → paid_work; Night class → other "
            "(or paid_work if memory/career context); Therapy → self_recovery.\n"
            "- conflicts: at most 3 actionable forward-looking sentences "
            "(what to protect / not double-book), citing real titles or people. "
            "Empty list is fine. Never retrospective 'indicates/previously scheduled' "
            "observations or role_id names (paid_work). Night class is not self_recovery.\n"
            "- facts: 1-4 first-person life statements (who you care for, constraints). "
            "Never list or quote calendar titles / meeting names in facts.\n"
            "Keep the JSON compact — short evidence strings.\n\n"
            f"Calendar titles:\n{titles_block}\n\n"
            f"Memory snippets:\n{facts_block}\n\n"
            f"Previous care roles (honor Rejected — do not revive):\n{prev_block}\n"
        )
        try:
            resp = await gemini.generate(
                GenerationRequest(
                    model_id=model,
                    prompt=prompt,
                    system_instruction=_SYSTEM_CARE,
                    response_schema=CareHolisticInfer.model_json_schema(),
                    temperature=0.1,
                    max_output_tokens=4096,
                    metadata={"task": "care_profile_infer", "title_cap": title_cap},
                )
            )
        except ModelUnavailable:
            _logger.info("care_holistic_unavailable", title_cap=title_cap)
            continue
        except Exception:  # noqa: BLE001
            _logger.exception("care_holistic_failed", title_cap=title_cap)
            continue
        text = (resp.text or "").strip()
        if not text:
            continue
        try:
            parsed = CareHolisticInfer.model_validate(json.loads(text))
        except Exception:  # noqa: BLE001
            _logger.warning(
                "care_holistic_parse_failed",
                title_cap=title_cap,
                preview=text[:160],
            )
            continue
        if parsed.roles or parsed.people or parsed.events:
            return parsed
    return None


def _merge_role_feedback(
    inferred: CareRoleState,
    previous: CareProfile | None,
) -> CareRoleState:
    if previous is None:
        return inferred
    for old in previous.roles:
        if old.role_id is not inferred.role_id:
            continue
        if old.status is BulletStatus.REJECTED:
            return inferred.model_copy(
                update={
                    "status": BulletStatus.REJECTED,
                    "salience": min(inferred.salience, 0.25),
                    "people": [],
                }
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


def _alias_to_canonical(inferred: CareHolisticInfer) -> dict[str, str]:
    """Map every nickname → canonical display name from people[].also_known_as."""
    mapping: dict[str, str] = {}
    for person in inferred.people:
        canon = _display_name(person.name)
        if not canon:
            continue
        mapping[canon.lower()] = canon
        for alias in person.also_known_as or []:
            a = _display_name(alias)
            if a:
                mapping[a.lower()] = canon
    return mapping


def _canonical_person_name(raw: str, alias_map: dict[str, str]) -> str:
    name = _display_name(raw)
    if not name:
        return ""
    return alias_map.get(name.lower(), name)


def care_profile_from_holistic(
    *,
    user_id: str,
    inferred: CareHolisticInfer,
    previous: CareProfile | None = None,
    event_titles: list[str] | None = None,
) -> tuple[CareProfile, list[Fact]]:
    """Build CareProfile + Facts from a holistic AI result."""
    alias_map = _alias_to_canonical(inferred)
    # People map (exclusive) — people[] is authoritative; one canonical label each.
    people_by_role: dict[CareRoleId, list[str]] = {
        CareRoleId.CHILD_CARE: [],
        CareRoleId.ELDER_CARE: [],
        CareRoleId.PARTNER_COPARENT: [],
    }
    claimed: set[str] = set()
    ordered = sorted(
        inferred.people,
        key=lambda p: (0 if "elder" in (p.role or "") else 1, p.name.lower()),
    )
    for person in ordered:
        name = _canonical_person_name(person.name, alias_map)
        if not name or len(name) > 40:
            continue
        key = name.lower()
        if key in claimed:
            continue
        role = _parse_role(person.role, allowed=_PERSON_ROLES)
        if role is None:
            continue
        claimed.add(key)
        for alias in person.also_known_as or []:
            a = _display_name(alias)
            if a:
                claimed.add(a.lower())
        people_by_role[role].append(name)

    # Role states from roles[] (preferred) else from people presence.
    role_specs: dict[CareRoleId, CareRoleInfer] = {}
    for raw in inferred.roles:
        rid = _parse_role(raw.role_id)
        if rid is None or not raw.present:
            continue
        # Skip roles previously rejected unless the model explicitly accepted —
        # merge_feedback still wins below.
        role_specs[rid] = raw

    # Ensure person roles exist when people were assigned.
    for rid, names in people_by_role.items():
        if names and rid not in role_specs:
            role_specs[rid] = CareRoleInfer(
                role_id=rid.value,
                salience=0.78,
                weekly_load_hours=4.0,
                people=names,
                evidence=f"Inferred people: {', '.join(names)}",
                present=True,
            )

    roles: list[CareRoleState] = []
    facts: list[Fact] = []
    for rid, spec in role_specs.items():
        raw_names = [*spec.people, *people_by_role.get(rid, [])]
        names = [
            _canonical_person_name(n, alias_map)
            for n in raw_names
            if n
        ]
        names = list(dict.fromkeys([n for n in names if n]))
        # Prefer people[] list for this role when present (already de-duped).
        if people_by_role.get(rid):
            names = list(
                dict.fromkeys(
                    [
                        *people_by_role[rid],
                        *[
                            n
                            for n in names
                            if n.lower() in {x.lower() for x in people_by_role[rid]}
                            or n.lower() not in claimed
                        ],
                    ]
                )
            )
        names = [
            n
            for n in names
            if n
            and (
                n.lower() not in claimed
                or n.lower() in {x.lower() for x in people_by_role.get(rid, [])}
            )
        ]
        # Enforce exclusivity on this role's people.
        clean_people: list[str] = []
        for n in names:
            owner = None
            for pr, plist in people_by_role.items():
                if n.lower() in {x.lower() for x in plist}:
                    owner = pr
                    break
            if owner is None or owner is rid:
                if n.lower() not in {x.lower() for x in clean_people}:
                    clean_people.append(n)
        sal = max(0.05, min(0.98, float(spec.salience or 0.7)))
        hours = max(0.0, min(60.0, float(spec.weekly_load_hours or 4.0)))
        evidence = (spec.evidence or "").strip()[:200]
        if not evidence and clean_people:
            evidence = f"{CARE_ROLE_LABELS[rid]} with {', '.join(clean_people)}"
        # Role evidence stays on CareRoleState for the graph — never mint
        # "I hold <calendar inventory>" Memory facts (those leaked into Today tips).
        role = CareRoleState(
            role_id=rid,
            label=CARE_ROLE_LABELS[rid],
            salience=sal,
            weekly_load_hours=hours,
            evidence_summaries=[evidence] if evidence else [],
            people=clean_people[:4],
            source_fact_ids=[],
        )
        roles.append(_merge_role_feedback(role, previous))

    # Preserve previously rejected roles that the model omitted (so they stay rejected).
    if previous:
        have = {r.role_id for r in roles}
        for old in previous.roles:
            if old.status is BulletStatus.REJECTED and old.role_id not in have:
                roles.append(
                    old.model_copy(
                        update={
                            "salience": min(old.salience, 0.2),
                            "people": [],
                            "weekly_load_hours": 0.0,
                        }
                    )
                )

    # Event hints.
    catalog = list(event_titles or [])
    hints: dict[str, str] = {}
    for ev in inferred.events:
        title = _match_title(ev.summary, catalog) if catalog else (ev.summary or "").strip()
        if not title:
            title = (ev.summary or "").strip()
        if not title:
            continue
        role = _parse_role(ev.role)
        if role is None:
            continue
        hints[_norm_title(title)] = role.value

    # Only the model's explicit first-person life facts — not role/calendar dumps.
    for stmt in inferred.facts[:4]:
        text = (stmt or "").strip()
        if len(text) < 12:
            continue
        if not text.lower().startswith("i "):
            text = f"I {text[0].lower()}{text[1:]}" if text else text
        facts.append(
            Fact(
                user_id=user_id,
                type=FactType.VALUE_STATEMENT,
                statement=text[:300],
                salience=0.7,
                confidence=0.8,
                source_signal_ids=[],
                written_by="care_infer_fact@v2",
            )
        )

    version = (previous.version + 1) if previous else 1
    person_rels: dict[str, str] = {}
    if previous and previous.person_relationships:
        person_rels.update(previous.person_relationships)
    for person in inferred.people:
        name = _canonical_person_name(person.name, alias_map)
        rel = " ".join((person.relationship or "").strip().split())
        if name and rel:
            person_rels[name] = rel[:48]

    # Guard: empty AI output must not erase Keep/Not-me roles already on file.
    if not roles and previous and previous.roles:
        profile = previous.model_copy(
            update={
                "version": version,
                "updated_at": datetime.now(tz=timezone.utc),
                "calendar_role_by_summary": {
                    **previous.calendar_role_by_summary,
                    **hints,
                },
                "conflict_summaries": clean_conflict_summaries(inferred.conflicts)
                or clean_conflict_summaries(previous.conflict_summaries),
                "person_relationships": person_rels or previous.person_relationships,
            }
        )
        return reconcile_exclusive_people(profile), facts

    live_names = {p.lower() for r in roles for p in r.people}
    if live_names:
        person_rels = {
            k: v for k, v in person_rels.items() if k.lower() in live_names
        }

    profile = CareProfile(
        user_id=user_id,
        roles=roles,
        version=version,
        updated_at=datetime.now(tz=timezone.utc),
        conflict_summaries=clean_conflict_summaries(inferred.conflicts),
        calendar_role_by_summary=hints,
        helpers=list(previous.helpers) if previous else [],
        person_relationships=person_rels,
    )
    return reconcile_exclusive_people(profile), facts


def apply_holistic_inference(
    profile: CareProfile,
    inferred: CareHolisticInfer,
    *,
    event_titles: list[str] | None = None,
) -> CareProfile:
    """Patch an existing profile with holistic people + event hints (compat)."""
    care, _facts = care_profile_from_holistic(
        user_id=profile.user_id,
        inferred=inferred,
        previous=profile,
        event_titles=event_titles,
    )
    return care


def reconcile_exclusive_people(profile: CareProfile) -> CareProfile:
    """Ensure each person label appears under at most one care role."""
    preference = {
        CareRoleId.ELDER_CARE: 0,
        CareRoleId.PARTNER_COPARENT: 1,
        CareRoleId.CHILD_CARE: 2,
        CareRoleId.HOUSEHOLD_LOGISTICS: 3,
        CareRoleId.PAID_WORK: 4,
        CareRoleId.SELF_RECOVERY: 5,
    }
    owners: dict[str, tuple[int, CareRoleId, float]] = {}
    for role in profile.roles:
        if role.status is BulletStatus.REJECTED:
            continue
        rank = preference.get(role.role_id, 9)
        for person in role.people:
            key = person.lower()
            prev = owners.get(key)
            score = (rank, -role.salience)
            if prev is None or score < (prev[0], -prev[2]):
                owners[key] = (rank, role.role_id, role.salience)

    roles: list[CareRoleState] = []
    changed = False
    for role in profile.roles:
        if role.status is BulletStatus.REJECTED or not role.people:
            roles.append(role)
            continue
        kept = [
            p
            for p in role.people
            if owners.get(p.lower(), (9, role.role_id, 0))[1] is role.role_id
        ]
        if kept != role.people:
            changed = True
            roles.append(role.model_copy(update={"people": kept}))
        else:
            roles.append(role)
    if not changed:
        return profile
    return profile.model_copy(
        update={
            "roles": roles,
            "version": profile.version + 1,
            "updated_at": datetime.now(tz=timezone.utc),
        }
    )


def _person_labels_need_consolidate(inferred: CareHolisticInfer) -> bool:
    """True when a role lists multiple person labels (likely nickname duplicates)."""
    by_role: dict[str, set[str]] = {}
    for person in inferred.people:
        role = (person.role or "").strip().lower()
        name = _display_name(person.name)
        if not role or not name:
            continue
        by_role.setdefault(role, set()).add(name.lower())
        for alias in person.also_known_as or []:
            a = _display_name(alias)
            if a:
                by_role[role].add(a.lower())
    for raw in inferred.roles:
        role = (raw.role_id or "").strip().lower()
        for n in raw.people or []:
            name = _display_name(n)
            if role and name:
                by_role.setdefault(role, set()).add(name.lower())
    return any(len(names) >= 2 for names in by_role.values())


async def consolidate_care_people_ai(
    gemini: GeminiClient,
    *,
    inferred: CareHolisticInfer,
    event_titles: list[str],
    previous: CareProfile | None = None,
    model_id: str | None = None,
) -> CareHolisticInfer:
    """Specialized AI wrapper: one human → one canonical name (+ also_known_as).

    Runs when holistic output still has multiple labels on the same role
    (e.g. Papa / Dad / Robert for one elder).
    """
    if not _person_labels_need_consolidate(inferred):
        return inferred

    settings = get_settings()
    model = model_id or settings.fast_model
    labels: list[str] = []
    for person in inferred.people:
        name = _display_name(person.name)
        if name:
            labels.append(f"{name} → {person.role}")
        for alias in person.also_known_as or []:
            a = _display_name(alias)
            if a:
                labels.append(f"{a} (alias of {name}) → {person.role}")
    for raw in inferred.roles:
        for n in raw.people or []:
            name = _display_name(n)
            if name:
                labels.append(f"{name} → {raw.role_id}")
    prev_people = []
    if previous:
        for r in previous.roles:
            if r.status is BulletStatus.REJECTED:
                continue
            for p in r.people:
                prev_people.append(f"{p} ({r.role_id.value})")
    titles_block = "\n".join(f"- {t}" for t in event_titles[:28]) or "(none)"
    labels_block = "\n".join(f"- {x}" for x in dict.fromkeys(labels)) or "(none)"
    prev_block = "\n".join(f"- {x}" for x in prev_people[:12]) or "(none)"
    prompt = (
        "Consolidate care recipients into ONE entry per human being.\n"
        "Calendar titles use nicknames and legal names for the same people "
        "(e.g. Papa, Dad, Robert Chen → one elder named Robert; also_known_as "
        "Papa, Dad).\n"
        "Return people: [{name, role, evidence, also_known_as[], relationship}].\n"
        "Rules: prefer a given name when known; kinship words alone (Dad, Papa, "
        "Mom) only if no given name appears; never emit three rows for one human; "
        "role in {child_care, elder_care, partner_coparent}; "
        "relationship = short phrase for how they relate to the caregiver "
        "(parent, child, co-parent, …) from context.\n\n"
        f"Labels from a prior pass:\n{labels_block}\n\n"
        f"Calendar titles:\n{titles_block}\n\n"
        f"Previous Care Profile people (prefer stable names):\n{prev_block}\n"
    )
    try:
        resp = await gemini.generate(
            GenerationRequest(
                model_id=model,
                prompt=prompt,
                system_instruction=(
                    "You merge duplicate person labels for a caregiver Care Profile. "
                    "Output JSON only. One human = one people entry."
                ),
                response_schema=CarePeopleConsolidate.model_json_schema(),
                temperature=0.0,
                max_output_tokens=900,
                metadata={"task": "care_people_consolidate"},
            )
        )
    except ModelUnavailable:
        _logger.info("care_people_consolidate_unavailable")
        return inferred
    except Exception:  # noqa: BLE001
        _logger.exception("care_people_consolidate_failed")
        return inferred
    text = (resp.text or "").strip()
    if not text:
        return inferred
    try:
        consolidated = CarePeopleConsolidate.model_validate(json.loads(text))
    except Exception:  # noqa: BLE001
        _logger.warning("care_people_consolidate_parse_failed", preview=text[:160])
        return inferred
    if not consolidated.people:
        return inferred

    # Rewrite roles.people to canonical names from the consolidate pass.
    alias_map: dict[str, str] = {}
    for person in consolidated.people:
        canon = _display_name(person.name)
        if not canon:
            continue
        alias_map[canon.lower()] = canon
        for alias in person.also_known_as or []:
            a = _display_name(alias)
            if a:
                alias_map[a.lower()] = canon
    new_roles: list[CareRoleInfer] = []
    for raw in inferred.roles:
        mapped = [
            alias_map.get(_display_name(n).lower(), _display_name(n))
            for n in (raw.people or [])
            if _display_name(n)
        ]
        # For person roles, prefer consolidate list for that role.
        rid = _parse_role(raw.role_id, allowed=_PERSON_ROLES)
        if rid is not None:
            mapped = [
                _display_name(p.name)
                for p in consolidated.people
                if _parse_role(p.role, allowed=_PERSON_ROLES) is rid
                and _display_name(p.name)
            ]
        mapped = list(dict.fromkeys([m for m in mapped if m]))
        new_roles.append(raw.model_copy(update={"people": mapped}))
    _logger.info(
        "care_people_consolidated",
        before=len(labels),
        after=len(consolidated.people),
    )
    return inferred.model_copy(
        update={"people": consolidated.people, "roles": new_roles}
    )


async def infer_care_profile_ai(
    *,
    user_id: str,
    events: list[dict[str, str | None]],
    previous: CareProfile | None = None,
    fact_snippets: list[str] | None = None,
    gemini: GeminiClient | None = None,
) -> tuple[CareProfile, list[Fact]] | None:
    """AI-first Care Profile build. Returns None if the model is unavailable."""
    client = gemini or build_gemini_client(get_settings())
    titles = _unique_titles(events)
    inferred = await infer_care_holistic(
        client,
        events=events,
        fact_snippets=fact_snippets,
        previous=previous,
    )
    if inferred is None:
        return None
    inferred = await consolidate_care_people_ai(
        client,
        inferred=inferred,
        event_titles=titles,
        previous=previous,
    )
    return care_profile_from_holistic(
        user_id=user_id,
        inferred=inferred,
        previous=previous,
        event_titles=titles,
    )


async def enrich_care_profile_holistic(
    profile: CareProfile,
    events: list[dict[str, str | None]],
    *,
    fact_snippets: list[str] | None = None,
    gemini: GeminiClient | None = None,
) -> CareProfile:
    """Re-infer / refresh an existing profile holistically."""
    result = await infer_care_profile_ai(
        user_id=profile.user_id,
        events=events,
        previous=profile,
        fact_snippets=fact_snippets,
        gemini=gemini,
    )
    if result is None:
        return reconcile_exclusive_people(profile)
    care, _facts = result
    # Never wipe a working Care Profile with an empty model response.
    if not care.roles and profile.roles:
        _logger.warning(
            "care_enrich_empty_kept_previous",
            user_id=profile.user_id,
            previous_roles=len(profile.roles),
        )
        merged_hints = {
            **profile.calendar_role_by_summary,
            **care.calendar_role_by_summary,
        }
        return reconcile_exclusive_people(
            profile.model_copy(
                update={
                    "calendar_role_by_summary": merged_hints,
                    "version": profile.version + 1,
                    "updated_at": datetime.now(tz=timezone.utc),
                }
            )
        )
    return care


async def apply_note_to_care_profile_ai(
    profile: CareProfile,
    note: str,
    *,
    gemini: GeminiClient | None = None,
) -> tuple[CareProfile, str] | None:
    """Mutate Care Profile from a free-text note via Gemini. None on failure."""
    text = (note or "").strip()
    if len(text) < 4:
        return None
    client = gemini or build_gemini_client(get_settings())
    settings = get_settings()
    current = []
    for r in profile.roles:
        peeps = f" ({', '.join(r.people)})" if r.people else ""
        current.append(f"- {r.role_id.value}: {r.status.value}, salience={r.salience:.2f}{peeps}")
    system = (
        "You update a caregiver's Care Profile from a short note they wrote. "
        "Infer intent holistically (not keyword matching). "
        "If they say they have no co-parent / are solo / just me, reject partner_coparent. "
        "Mom/Dad kinship is elder_care, never child_care. "
        "IMPORTANT: occasional friends/neighbors who help drive, babysit, or pick up "
        "are helpers — NOT child_care people and NOT partner_coparent. "
        "Only add helpers[] when the note clearly names a helper; never invent "
        "anonymous Friend rows or placeholder care-recipient names. "
        "Put named helpers in helpers with helps=[child/elder name from the note or profile]. "
        "Care recipients go in people. "
        "Each care-recipient person one role. Reply warmly in 1–2 short complete "
        "sentences (never trail off), no quotation marks wrapping the reply."
    )
    prompt = (
        "Return JSON with:\n"
        "- reply: brief confirmation (1 sentence)\n"
        "- reject_roles: role ids to mark Rejected "
        "(child_care|elder_care|paid_work|self_recovery|household_logistics|partner_coparent)\n"
        "- accept_roles: role ids to strengthen / Accept\n"
        "- people: [{name, role, evidence, relationship}] care recipients only "
        "(relationship = short phrase: parent, child, co-parent, …) "
        "(child_care|elder_care|partner_coparent)\n"
        "- helpers: [{name, helps[], hint, helps_role}] for friends/neighbors who "
        "occasionally help — helps lists who they help (e.g. [\"Jordan\"])\n"
        "- evidence: optional short note\n"
        "- conflicts: optional actionable forward-looking tips only "
        "(skip history/observations; no role_id jargon)\n\n"
        f"Current roles:\n{chr(10).join(current) or '(empty)'}\n"
        f"Current helpers: {', '.join(h.name for h in profile.helpers) or '(none)'}\n\n"
        f"User note:\n{text}\n"
    )
    try:
        resp = await client.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                prompt=prompt,
                system_instruction=system,
                response_schema=CareNoteUpdate.model_json_schema(),
                temperature=0.1,
                max_output_tokens=320,
                metadata={"task": "care_note_update"},
            )
        )
    except ModelUnavailable:
        return None
    except Exception:  # noqa: BLE001
        _logger.exception("care_note_update_failed")
        return None
    raw = (resp.text or "").strip()
    if not raw:
        return None
    try:
        update = CareNoteUpdate.model_validate(json.loads(raw))
    except Exception:  # noqa: BLE001
        _logger.warning("care_note_parse_failed", preview=raw[:160])
        return None

    by_id = {r.role_id: r for r in profile.roles}

    for rid_raw in update.reject_roles:
        rid = _parse_role(rid_raw)
        if rid is None:
            continue
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = CareRoleState(
                role_id=rid,
                label=CARE_ROLE_LABELS[rid],
                salience=0.1,
                weekly_load_hours=0.0,
                status=BulletStatus.REJECTED,
                evidence_summaries=[f"Marked not you: {CARE_ROLE_LABELS[rid]}"],
                people=[],
            )
        else:
            by_id[rid] = existing.model_copy(
                update={
                    "status": BulletStatus.REJECTED,
                    "salience": min(existing.salience, 0.2),
                    "weekly_load_hours": 0.0,
                    "people": [],
                    "evidence_summaries": [
                        f"Marked not you: {CARE_ROLE_LABELS[rid]}",
                        *existing.evidence_summaries,
                    ][:4],
                }
            )

    for rid_raw in update.accept_roles:
        rid = _parse_role(rid_raw)
        if rid is None:
            continue
        existing = by_id.get(rid)
        if existing is not None and existing.status is BulletStatus.REJECTED:
            # Explicit reject wins over vague accept in the same note unless
            # reject list didn't include it — still don't un-reject weakly.
            if rid.value in {x.lower() for x in update.reject_roles}:
                continue
        role_evidence = (
            (update.evidence or "").strip()
            if (update.evidence or "").strip() and len((update.evidence or "").strip()) <= 120
            else f"Holding {CARE_ROLE_LABELS[rid]}"
        )
        if existing is None:
            by_id[rid] = CareRoleState(
                role_id=rid,
                label=CARE_ROLE_LABELS[rid],
                salience=0.8,
                weekly_load_hours=2.0,
                status=BulletStatus.ACCEPTED,
                evidence_summaries=[role_evidence[:120]],
                people=[],
            )
        else:
            by_id[rid] = existing.model_copy(
                update={
                    "status": BulletStatus.ACCEPTED
                    if existing.status is not BulletStatus.REJECTED
                    else existing.status,
                    "salience": min(0.98, max(existing.salience, 0.8) + 0.05)
                    if existing.status is not BulletStatus.REJECTED
                    else existing.salience,
                    "evidence_summaries": [
                        role_evidence[:120],
                        *existing.evidence_summaries,
                    ][:4],
                }
            )

    claimed: set[str] = set()
    helper_names: set[str] = set()
    for h in update.helpers:
        hn = _display_name(h.name)
        if hn:
            helper_names.add(hn.lower())
    # Never treat occasional helpers as care recipients / held roles.
    for person in sorted(update.people, key=lambda p: (0 if "elder" in p.role else 1, p.name)):
        name = _display_name(person.name)
        rid = _parse_role(person.role, allowed=_PERSON_ROLES)
        if not name or rid is None:
            continue
        if name.lower() in helper_names:
            continue
        # Generic "Friend" on child_care is almost always a helper mis-tag.
        if name.lower() in {"friend", "a friend", "neighbor"} and rid is CareRoleId.CHILD_CARE:
            helper_names.add(name.lower())
            continue
        if name.lower() in claimed:
            continue
        claimed.add(name.lower())
        existing = by_id.get(rid)
        if existing is not None and existing.status is BulletStatus.REJECTED:
            continue
        if existing is None:
            by_id[rid] = CareRoleState(
                role_id=rid,
                label=CARE_ROLE_LABELS[rid],
                salience=0.78,
                weekly_load_hours=2.0,
                status=BulletStatus.ACCEPTED,
                people=[name],
                evidence_summaries=[
                    (person.evidence or f"{name} — {CARE_ROLE_LABELS[rid]}")[:120]
                ],
            )
        else:
            peeps = [p for p in existing.people if p.lower() != name.lower()]
            person_ev = (person.evidence or f"{name} — {CARE_ROLE_LABELS[rid]}")[:120]
            by_id[rid] = existing.model_copy(
                update={
                    "people": [name, *peeps][:4],
                    "evidence_summaries": [person_ev, *existing.evidence_summaries][:4],
                }
            )

    # Strip claimed / helper names from other roles.
    for role in list(by_id.values()):
        if not role.people:
            continue
        owner_ok = {
            _display_name(p.name).lower()
            for p in update.people
            if _parse_role(p.role, allowed=_PERSON_ROLES) is role.role_id
            and _display_name(p.name).lower() not in helper_names
        }
        cleaned = [
            p
            for p in role.people
            if p.lower() not in helper_names
            and (p.lower() not in claimed or p.lower() in owner_ok)
        ]
        if cleaned != role.people:
            by_id[role.role_id] = role.model_copy(update={"people": cleaned})

    # Merge helpers only when Gemini named them — never invent Friend/Jordan rows.
    helpers_by_name = {h.name.lower(): h for h in profile.helpers}
    for raw_h in update.helpers:
        name = _display_name(raw_h.name)
        if not name:
            continue
        helps = [_display_name(x) for x in (raw_h.helps or []) if x]
        helps = [x for x in helps if x][:4]
        if not helps:
            child = by_id.get(CareRoleId.CHILD_CARE)
            if child and child.people:
                helps = list(child.people[:2])
        if not helps:
            # No recipient to attach — skip rather than invent a placeholder.
            continue
        hint = (raw_h.hint or "Occasionally helps with care").strip()[:160]
        helps_role = (raw_h.helps_role or "child_care").strip() or "child_care"
        helpers_by_name[name.lower()] = CareHelper(
            name=name,
            helps=helps,
            hint=hint,
            helps_role=helps_role,
        )

    conflicts = clean_conflict_summaries(
        list(update.conflicts) + list(profile.conflict_summaries)
    )
    person_rels = dict(profile.person_relationships)
    for person in update.people:
        name = _display_name(person.name)
        rel = " ".join((person.relationship or "").strip().split())
        if name and rel and name.lower() not in helper_names:
            person_rels[name] = rel[:48]
    care = profile.model_copy(
        update={
            "roles": list(by_id.values()),
            "helpers": list(helpers_by_name.values())[:6],
            "conflict_summaries": conflicts,
            "person_relationships": person_rels,
            "version": profile.version + 1,
            "updated_at": datetime.now(tz=timezone.utc),
        }
    )
    care = reconcile_exclusive_people(care)
    # Helpers must never appear as held care recipients.
    helper_keys = {h.name.lower() for h in care.helpers}
    if helper_keys:
        cleaned_roles = []
        for role in care.roles:
            peeps = [p for p in role.people if p.lower() not in helper_keys]
            cleaned_roles.append(
                role if peeps == role.people else role.model_copy(update={"people": peeps})
            )
        care = care.model_copy(update={"roles": cleaned_roles})
    reply = (update.reply or "").strip().strip("\"'")
    if len(reply) < 8:
        reply = "Got it — I updated your care roles from what you told me."
    return care, reply


async def classify_week_event_roles_ai(
    *,
    week_events: list[dict[str, str | None]],
    profile: CareProfile | None,
    gemini: GeminiClient | None = None,
    model_id: str | None = None,
) -> dict[str, str]:
    """Specialized AI wrapper: classify this week's calendar into care roles.

    Uses the caregiver's known roles + week titles. Returns normalized
    title → role_id for merging into ``calendar_role_by_summary``.
    """
    titles = _unique_titles(week_events, limit=36)
    if not titles:
        return {}
    client = gemini or build_gemini_client(get_settings())
    settings = get_settings()
    model = model_id or settings.fast_model

    role_lines: list[str] = []
    if profile and profile.roles:
        for r in profile.roles:
            if r.status is BulletStatus.REJECTED:
                continue
            peeps = f" (people: {', '.join(r.people[:4])})" if r.people else ""
            role_lines.append(f"- {r.role_id.value}: {CARE_ROLE_LABELS[r.role_id]}{peeps}")
    roles_block = "\n".join(role_lines) or "\n".join(
        f"- {rid.value}: {CARE_ROLE_LABELS[rid]}" for rid in CareRoleId
    )
    titles_block = "\n".join(f"- {t}" for t in titles)
    prompt = (
        "Classify each calendar event into ONE care role for this busy caregiver.\n"
        "Use the care roles they hold + the event titles. Reason holistically — "
        "do not keyword-match naively.\n"
        "Valid role ids: child_care, elder_care, paid_work, self_recovery, "
        "household_logistics, partner_coparent, other.\n"
        "Meeting / call / sync / 1:1 → paid_work unless clearly personal.\n"
        "School pickup / soccer / kid activities → child_care.\n"
        "Mom/Dad medical or visits → elder_care.\n"
        "Therapy / sleep / recovery for the caregiver → self_recovery.\n"
        "Groceries / forms / errands → household_logistics.\n"
        "Classes/courses are paid_work if career, else other — never self_recovery.\n"
        "Return JSON: events[{summary, role}] using the exact title strings.\n\n"
        f"Care roles held:\n{roles_block}\n\n"
        f"This week's events:\n{titles_block}\n"
    )
    try:
        resp = await client.generate(
            GenerationRequest(
                model_id=model,
                prompt=prompt,
                system_instruction=(
                    "You are Level. Assign each calendar title to a care role. "
                    "JSON only. No lectures."
                ),
                response_schema=WeekEventClassifyOut.model_json_schema(),
                temperature=0.1,
                max_output_tokens=900,
                metadata={"task": "week_event_classify"},
            )
        )
    except ModelUnavailable:
        _logger.info("week_event_classify_unavailable")
        return {}
    except Exception:  # noqa: BLE001
        _logger.exception("week_event_classify_failed")
        return {}

    text = (resp.text or "").strip()
    if not text:
        return {}
    try:
        parsed = WeekEventClassifyOut.model_validate(json.loads(text))
    except Exception:  # noqa: BLE001
        _logger.warning("week_event_classify_parse_failed", preview=text[:160])
        return {}

    out: dict[str, str] = {}
    title_set = {_norm_title(t) for t in titles}
    for item in parsed.events:
        key = _norm_title(item.summary)
        role = (item.role or "").strip().lower()
        if not key or key not in title_set:
            continue
        if role not in _ALL_ROLES:
            continue
        out[key] = role
    _logger.info("week_event_classify_done", titles=len(titles), tagged=len(out))
    return out


__all__ = [
    "CareEventAssign",
    "CareHelperAssign",
    "CareHolisticInfer",
    "CareNoteUpdate",
    "CarePeopleConsolidate",
    "CarePersonAssign",
    "CareRoleInfer",
    "WeekEventClassifyOut",
    "apply_holistic_inference",
    "apply_note_to_care_profile_ai",
    "care_profile_from_holistic",
    "classify_week_event_roles_ai",
    "consolidate_care_people_ai",
    "enrich_care_profile_holistic",
    "infer_care_holistic",
    "infer_care_profile_ai",
    "reconcile_exclusive_people",
]
