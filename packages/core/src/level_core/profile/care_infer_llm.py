"""AI-first Care Profile inference.

Pipeline: calendar / notes / memory snippets → Gemini structured infer →
Care Profile mutation. Regex heuristics live only as offline fallback in
``synthesize.infer_care_profile_heuristic``.
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
    name: str = ""
    role: str = "child_care"
    evidence: str = ""


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
    "- Mom/Dad/Mother/Father/Grandma/Grandpa are elder_care, never child_care.\n"
    "- Titles like \"Pharmacy pickup — Mom's meds\" or \"dinner drop-off for Mom\" "
    "are elder_care events.\n"
    "- Only include partner_coparent if there is real evidence of a co-parent/"
    "partner sharing child care; solo parents must omit it.\n"
    "- Salience 0..1 reflects how load-bearing the role is.\n"
    "- weekly_load_hours is a rough estimate.\n"
    "- facts: short first-person statements suitable for Memory Bank.\n"
    "- conflicts: real care collisions (work vs pickup, night class vs care, etc.). "
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
    """One structured call: full Care Profile + event categories + facts."""
    titles = _unique_titles(events)
    if not titles and not (fact_snippets or []):
        return None
    settings = get_settings()
    model = model_id or settings.fast_model
    facts_block = "\n".join(f"- {s}" for s in (fact_snippets or [])[:16]) or "(none)"
    titles_block = "\n".join(f"- {t}" for t in titles) or "(no calendar titles)"
    prev_block = "(none)"
    if previous and previous.roles:
        lines = []
        for r in previous.roles:
            st = r.status.value
            peeps = f" people={','.join(r.people)}" if r.people else ""
            lines.append(f"- {r.role_id.value} status={st} salience={r.salience:.2f}{peeps}")
        prev_block = "\n".join(lines)
    prompt = (
        "Infer this caregiver's Care Profile from the data below.\n"
        "Classify from the combination of calendar titles AND memory snippets — "
        "memory can resolve ambiguous titles.\n"
        "Return JSON with:\n"
        "- roles: list of {role_id, salience, weekly_load_hours, people[], evidence, present}\n"
        "  Include a role only when present=true and there is real evidence.\n"
        "- people: named people with role in {child_care, elder_care, partner_coparent} "
        "and short evidence (must agree with roles.people).\n"
        "- events: classify EACH calendar title into "
        "{child_care, elder_care, paid_work, self_recovery, household_logistics, "
        "partner_coparent, other}; use the exact title string.\n"
        "  Examples: Meeting/Call/Sync/1:1 → paid_work; Night class → other "
        "(or paid_work if memory/career context); Therapy → self_recovery.\n"
        "- conflicts: short strings of colliding loads "
        "(name the real loads — night class is not self_recovery).\n"
        "- facts: 1-6 first-person Memory Bank statements.\n\n"
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
                max_output_tokens=2200,
                metadata={"task": "care_profile_infer"},
            )
        )
    except ModelUnavailable:
        _logger.info("care_holistic_unavailable")
        return None
    except Exception:  # noqa: BLE001
        _logger.exception("care_holistic_failed")
        return None
    text = (resp.text or "").strip()
    if not text:
        return None
    try:
        return CareHolisticInfer.model_validate(json.loads(text))
    except Exception:  # noqa: BLE001
        _logger.warning("care_holistic_parse_failed", preview=text[:160])
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


def care_profile_from_holistic(
    *,
    user_id: str,
    inferred: CareHolisticInfer,
    previous: CareProfile | None = None,
    event_titles: list[str] | None = None,
) -> tuple[CareProfile, list[Fact]]:
    """Build CareProfile + Facts from a holistic AI result."""
    # People map (exclusive).
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
        name = _display_name(person.name)
        if not name or len(name) > 40:
            continue
        key = name.lower()
        if key in claimed:
            continue
        role = _parse_role(person.role, allowed=_PERSON_ROLES)
        if role is None:
            continue
        claimed.add(key)
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
        names = list(dict.fromkeys([*spec.people, *people_by_role.get(rid, [])]))
        names = [_display_name(n) for n in names if n]
        names = [n for n in names if n and (n.lower() not in claimed or n.lower() in {x.lower() for x in people_by_role.get(rid, [])})]
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
        if evidence:
            facts.append(
                Fact(
                    user_id=user_id,
                    type=FactType.RELATIONSHIP
                    if rid
                    in {
                        CareRoleId.CHILD_CARE,
                        CareRoleId.ELDER_CARE,
                        CareRoleId.PARTNER_COPARENT,
                    }
                    else FactType.COMMITMENT,
                    statement=evidence[:300]
                    if evidence.lower().startswith("i ")
                    else f"I hold {evidence[:280]}",
                    salience=sal,
                    confidence=0.82,
                    source_signal_ids=[],
                    written_by="care_infer@v2",
                )
            )
        role = CareRoleState(
            role_id=rid,
            label=CARE_ROLE_LABELS[rid],
            salience=sal,
            weekly_load_hours=hours,
            evidence_summaries=[evidence] if evidence else [],
            people=clean_people[:4],
            source_fact_ids=[facts[-1].fact_id] if facts else [],
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

    # Extra facts from model.
    for stmt in inferred.facts[:6]:
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
                written_by="care_infer@v2",
            )
        )

    version = (previous.version + 1) if previous else 1
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
                "conflict_summaries": [
                    c.strip()[:200] for c in inferred.conflicts if c.strip()
                ][:4]
                or previous.conflict_summaries,
            }
        )
        return reconcile_exclusive_people(profile), facts

    profile = CareProfile(
        user_id=user_id,
        roles=roles,
        version=version,
        updated_at=datetime.now(tz=timezone.utc),
        conflict_summaries=[c.strip()[:200] for c in inferred.conflicts if c.strip()][:4],
        calendar_role_by_summary=hints,
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
        "Put them in helpers with helps=[child name]. "
        "Care recipients (Jordan, Mom) go in people. "
        "Each care-recipient person one role. Reply warmly in 1–2 short complete "
        "sentences (never trail off), no quotation marks wrapping the reply."
    )
    prompt = (
        "Return JSON with:\n"
        "- reply: brief confirmation (1 sentence)\n"
        "- reject_roles: role ids to mark Rejected "
        "(child_care|elder_care|paid_work|self_recovery|household_logistics|partner_coparent)\n"
        "- accept_roles: role ids to strengthen / Accept\n"
        "- people: [{name, role, evidence}] care recipients only "
        "(child_care|elder_care|partner_coparent)\n"
        "- helpers: [{name, helps[], hint, helps_role}] for friends/neighbors who "
        "occasionally help — helps lists who they help (e.g. [\"Jordan\"])\n"
        "- evidence: optional short note\n"
        "- conflicts: optional new conflict strings\n\n"
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

    # Merge helpers (friends who help Jordan, etc.).
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
        hint = (raw_h.hint or "Occasionally helps with care").strip()[:160]
        helps_role = (raw_h.helps_role or "child_care").strip() or "child_care"
        helpers_by_name[name.lower()] = CareHelper(
            name=name,
            helps=helps,
            hint=hint,
            helps_role=helps_role,
        )
    # Note implies a friend helper but model omitted helpers[].
    if not update.helpers and re.search(
        r"\b(friend|neighbor).{0,80}\b(help|drive|carpool|pickup|practice)\b"
        r"|\b(help|drive|carpool).{0,80}\b(friend|neighbor)\b",
        text,
        re.I,
    ):
        child = by_id.get(CareRoleId.CHILD_CARE)
        helps = list((child.people if child else [])[:2])
        for token in re.findall(r"\b([A-Z][a-z]{2,})\b", note):
            if _is_likely_name(token) and token.lower() not in {
                "friend",
                "neighbor",
                "occasionally",
            }:
                # Prefer explicit care-recipient names already on child_care.
                if child and any(token.lower() == p.lower() for p in child.people):
                    helps = [token]
                    break
                if token.lower() not in helpers_by_name and not helps:
                    helps = [token]
        helpers_by_name.setdefault(
            "friend",
            CareHelper(
                name="Friend",
                helps=helps[:2] or ["Jordan"],
                hint="Occasionally helps with child care",
                helps_role="child_care",
            ),
        )

    conflicts = list(profile.conflict_summaries)
    for c in update.conflicts:
        c = c.strip()
        if c and c not in conflicts:
            conflicts.insert(0, c[:200])
    care = profile.model_copy(
        update={
            "roles": list(by_id.values()),
            "helpers": list(helpers_by_name.values())[:6],
            "conflict_summaries": conflicts[:4],
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


def _is_likely_name(token: str) -> bool:
    t = (token or "").strip()
    if len(t) < 2 or len(t) > 24:
        return False
    return t[0].isupper() and t[1:].islower() and t.lower() not in {
        "friend",
        "practice",
        "school",
        "soccer",
        "pickup",
        "drive",
        "help",
        "helps",
        "neighbor",
        "occasionally",
    }


__all__ = [
    "CareEventAssign",
    "CareHelperAssign",
    "CareHolisticInfer",
    "CareNoteUpdate",
    "CarePersonAssign",
    "CareRoleInfer",
    "apply_holistic_inference",
    "apply_note_to_care_profile_ai",
    "care_profile_from_holistic",
    "enrich_care_profile_holistic",
    "infer_care_holistic",
    "infer_care_profile_ai",
    "reconcile_exclusive_people",
]
