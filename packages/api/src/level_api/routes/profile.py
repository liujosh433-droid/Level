"""Profile page: care people + usuals + priorities + Keep/Not me feedback."""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from level_core.agents.role import ProposedPerson
from level_core.agents.role import run as role_run
from level_core.agents.usual import run as usual_run
from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.sync import refresh_agenda
from level_core.calendar.usuals import compute_usuals_from_events, rollup_for_role_agent
from level_core.config import get_settings
from level_core.observability import get_logger
from level_core.tz import tz_for_store
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CareRelation,
    NegativeAgent,
    Priority,
    Usual,
    UsualStatus,
)
from level_core.storage.base import UserStore
from level_core.storage.care_store import (
    ensure_self_person,
    propose_person,
    propose_usual,
    record_negative,
    set_person_status,
    set_usual_status,
    sync_usuals,
)
from pydantic import BaseModel

from level_api.deps import get_user_store

router = APIRouter()
logger = get_logger(__name__)


class KeepNotMeBody(BaseModel):
    entity: str  # person | usual | priority
    id: str
    status: str  # kept | not_me


class DirectPersonAdd(BaseModel):
    display_name: str
    relation: CareRelation
    is_self: bool = False


class DirectPriorityAdd(BaseModel):
    text: str
    weight: int = 3
    activity_types: list[ActivityType] = []


@router.get("")
async def get_profile(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    settings = get_settings()
    people_models = await store.people.list()
    usuals_models = await store.usuals.list()
    priorities_models = await store.priorities.list()
    events = await store.agenda.list()

    people_by_id = {p.person_id: p for p in people_models}
    events_by_id = {e.event_id: e for e in events}
    tz = await tz_for_store(store)

    people = [p.model_dump(mode="json") for p in people_models]
    priorities = [p.model_dump(mode="json") for p in priorities_models]
    usuals = [
        _decorate_usual(u.model_dump(mode="json"), u.source_event_uids, u.person_id, events_by_id, people_by_id, tz)
        for u in usuals_models
    ]

    now_local = datetime.now(tz)
    week_keys: set[tuple[int, int]] = set()
    past_events = 0
    for e in events:
        if e.time.all_day:
            continue
        local = e.time.start.astimezone(tz)
        if local >= now_local:
            continue
        past_events += 1
        iso = local.isocalendar()
        week_keys.add((iso.year, iso.week))
    # Usuals are derived from past events only (future ones are plans,
    # not evidence), so the meta counts should reflect that too.
    usuals_meta = {
        "days_back": settings.level_cal_days_back,
        "weeks_observed": len(week_keys),
        "events_scanned": past_events,
        "min_repeats": 2,
    }
    return {"people": people, "usuals": usuals, "priorities": priorities, "usuals_meta": usuals_meta}


def _decorate_usual(
    dumped: dict[str, Any],
    source_event_uids: list[str],
    person_id: str,
    events_by_id: dict[str, CachedEvent],
    people_by_id: dict[str, Any],
    tz: ZoneInfo,
) -> dict[str, Any]:
    """Attach human-friendly typical start/end + person label to a usual."""
    starts: list[int] = []
    durations: list[int] = []
    for uid in source_event_uids:
        ev = events_by_id.get(uid)
        if not ev or ev.time.all_day:
            continue
        local_start = ev.time.start.astimezone(tz)
        local_end = ev.time.end.astimezone(tz)
        starts.append(local_start.hour * 60 + local_start.minute)
        durations.append(max(15, int((local_end - local_start).total_seconds() // 60)))

    if starts:
        typical_start_min = int(median(starts))
        typical_dur_min = int(median(durations))
        dumped["typical_start"] = _fmt_hm(typical_start_min)
        dumped["typical_end"] = _fmt_hm(typical_start_min + typical_dur_min)
    else:
        dumped["typical_start"] = None
        dumped["typical_end"] = None

    person = people_by_id.get(person_id)
    if person is not None:
        dumped["person_name"] = person.display_name
        dumped["person_relation"] = person.relation.value
    else:
        dumped["person_name"] = None
        dumped["person_relation"] = None
    return dumped


def _fmt_hm(total_minutes: int) -> str:
    total_minutes = max(0, min(23 * 60 + 59, total_minutes))
    hour_24 = (total_minutes // 60) % 24
    minute = total_minutes % 60
    suffix = "am" if hour_24 < 12 else "pm"
    hour_12 = hour_24 % 12 or 12
    if minute == 0:
        return f"{hour_12}{suffix}"
    return f"{hour_12}:{minute:02d}{suffix}"


@router.post("/refresh")
async def refresh_profile(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    """Re-read the calendar, then re-analyze people + usuals.

    Perf model (~10x speedup on a no-op rescan):
      1. Pull Google incrementally (syncToken -> ~0-5 events on rescan).
      2. If nothing changed since the LAST role_run AND every event is
         already classified, return immediately - no LLM calls, no
         re-enrich, no usuals rebuild. Rescan-with-no-changes goes from
         ~20s (previous behavior) to <500ms.
      3. Otherwise run the full enrich + role_run + usuals pipeline.
         `role_run` is skipped when the fingerprint hasn't moved since
         its last successful run (saves the ~2-5s LLM call).
    """
    await ensure_self_person(store)

    # Step 1: pull fresh data from Google. The button labeled "Re-read
    # your calendar" was previously misleading - it only re-analyzed the
    # cache without hitting Google. Now it does what it says, and thanks
    # to persistent syncToken this is typically <500ms.
    refresh_error: str | None = None
    current_fp: str = ""
    try:
        refresh_result = await refresh_agenda(store)
        current_fp = refresh_result.fingerprint
    except Exception as exc:  # noqa: BLE001
        refresh_error = str(exc)[:200]
        logger.warning("profile.refresh_agenda_failed", error=refresh_error)

    events = await store.agenda.list()
    tz = await tz_for_store(store)

    # Step 2: fingerprint short-circuit. Only fire when the Google pull
    # actually succeeded - otherwise we'd falsely tell the user "up to
    # date" while serving stale data.
    sync_state = await store.calendar_sync.read() or {}
    last_role_fp = sync_state.get("last_role_run_fingerprint")
    all_classified = all(
        e.activity_type is not None for e in events if e.summary
    )
    if (
        refresh_error is None
        and last_role_fp
        and current_fp
        and last_role_fp == current_fp
        and all_classified
    ):
        usuals = await store.usuals.list()
        return {
            "people_added": 0,
            "usuals_added": 0,
            "usuals_removed": 0,
            "up_to_date": True,
            "events_scanned": len(events),
            "usuals_total": len(usuals),
        }

    # Step 3: full pipeline.
    try:
        await enrich_agenda(store)
    except Exception as exc:  # noqa: BLE001 - enrich is best-effort
        logger.warning("profile.enrich_failed", error=str(exc)[:200])
    events = await store.agenda.list()

    # Skip role_run when the event set hasn't changed since its last
    # successful run. Even without a full short-circuit above, we still
    # avoid the expensive LLM call if e.g. a single event got classified
    # but the underlying calendar is identical.
    people_added = 0
    if last_role_fp != current_fp or not last_role_fp:
        rollup = rollup_for_role_agent(events, tz=tz)
        role_result = await role_run(store=store, calendar_rollup=rollup)
        if role_result.value:
            for pp in role_result.value.people:  # type: ignore[union-attr]
                assert isinstance(pp, ProposedPerson)
                await propose_person(
                    store,
                    display_name=pp.display_name,
                    relation=pp.relation,
                    aliases=pp.aliases,
                    is_self=pp.is_self,
                    source_span=pp.source_span,
                )
                people_added += 1

    # If we just added new people, matched_person_ids on existing events
    # points at [self_id] as fallback. Re-enrich so usuals below get built
    # against the real people, not Me. See docs/STATE_AND_LIFECYCLE.md §2.
    if people_added:
        try:
            await enrich_agenda(store)
            events = await store.agenda.list()
        except Exception:  # noqa: BLE001 - enrich is best-effort
            pass

    people = await store.people.list()
    candidates = compute_usuals_from_events(events, people, tz=tz)

    # Bulk usuals upsert instead of per-candidate get+put. Previously N
    # candidates = 2N doc ops; now 1 read + 1 upsert_many.
    fresh_ids: set[str] = set()
    existing_usuals = {u.usual_id: u for u in await store.usuals.list()}
    usuals_to_write: list[Usual] = []
    for c in candidates:
        usual_id = Usual.compose_id(c.person_id, c.weekday, c.hour_band)
        fresh_ids.add(usual_id)
        prior = existing_usuals.get(usual_id)
        if prior and prior.status == UsualStatus.NOT_ME:
            continue
        payload = Usual(
            usual_id=usual_id,
            person_id=c.person_id,
            weekday=c.weekday,
            hour_band=c.hour_band,
            activity_type=c.activity_type,
            display_summary=c.display_summary,
            source_event_uids=list(c.source_event_uids),
            confidence=c.confidence,
            status=prior.status if prior else UsualStatus.PROPOSED,
        )
        if prior != payload:
            usuals_to_write.append(payload)
    if usuals_to_write:
        await store.usuals.upsert_many(usuals_to_write)

    usuals_removed = await sync_usuals(store, fresh_ids)

    # Only remember the fingerprint when Google actually succeeded AND
    # we ran role_run. `update_fields` merges just this one key so we
    # don't clobber a concurrent refresh's `sync_tokens`.
    if current_fp and refresh_error is None and last_role_fp != current_fp:
        await store.calendar_sync.update_fields(
            last_role_run_fingerprint=current_fp
        )

    return {
        "people_added": people_added,
        # `usuals_added` == freshly written usuals (excludes ones that
        # already existed unchanged). Was previously len(candidates),
        # which overcounted every no-op refresh.
        "usuals_added": len(usuals_to_write),
        "usuals_removed": usuals_removed,
        "up_to_date": False,
        "refresh_error": refresh_error,
    }


@router.post("/keep_not_me")
async def keep_not_me(
    body: KeepNotMeBody, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    if body.entity == "person":
        updated = await set_person_status(store, body.id, body.status)
        if body.status == "not_me" and updated:
            await record_negative(
                store,
                agent=NegativeAgent.ROLE,
                field="display_name",
                value=updated.display_name,
            )
        return {"ok": bool(updated), "entity": "person"}

    if body.entity == "usual":
        status = UsualStatus(body.status)
        updated_u = await set_usual_status(store, body.id, status)
        if status == UsualStatus.NOT_ME and updated_u:
            await record_negative(
                store,
                agent=NegativeAgent.USUAL,
                field="display_summary",
                value=updated_u.display_summary,
            )
        return {"ok": bool(updated_u), "entity": "usual"}

    if body.entity == "priority":
        prio = await store.priorities.get(body.id)
        if not prio:
            raise HTTPException(status_code=404, detail="priority_not_found")
        updated_p = await store.priorities.upsert(
            prio.model_copy(update={"status": body.status})
        )
        if body.status == "not_me":
            await record_negative(
                store,
                agent=NegativeAgent.PRIORITY,
                field="text",
                value=updated_p.text,
            )
        return {"ok": True, "entity": "priority"}

    raise HTTPException(status_code=400, detail="bad_entity")


@router.post("/people")
async def add_person(
    body: DirectPersonAdd, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    person = await propose_person(
        store,
        display_name=body.display_name,
        relation=body.relation,
        is_self=body.is_self,
    )
    await set_person_status(store, person.person_id, "kept")
    return person.model_dump(mode="json")


@router.delete("/priorities/{priority_id}")
async def delete_priority(
    priority_id: str, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    """Remove a priority the user no longer wants Level to weigh.

    We delete the row (not just mark not_me) so it disappears from About Me
    and from booking conflict checks. A negative is recorded so PriorityAgent
    won't quietly re-extract the same sentence on the next chat turn.
    """
    prio = await store.priorities.get(priority_id)
    if prio is None:
        raise HTTPException(status_code=404, detail="priority_not_found")
    await record_negative(
        store,
        agent=NegativeAgent.PRIORITY,
        field="text",
        value=prio.text,
        reason="user deleted",
    )
    await store.priorities.delete(priority_id)
    return {"ok": True, "priority_id": priority_id}


@router.post("/priorities")
async def add_priority_direct(
    body: DirectPriorityAdd, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    prio = Priority(
        priority_id=f"prio_{store.user_id[:6]}",
        text=body.text.strip(),
        weight=body.weight,
        activity_types=body.activity_types,
        source="profile",
    )
    written = await store.priorities.upsert(prio)
    return written.model_dump(mode="json")


@router.post("/disambiguate")
async def disambiguate(
    candidates: list[dict[str, Any]], store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    result = await usual_run(store=store, candidates=candidates)
    return {"picks": [p.model_dump(mode="json") for p in (result.value.picks if result.value else [])]}  # type: ignore[union-attr]
