"""Async role-theft challenge job — Continuous Action Engine.

Scans each user's Care Profile + upcoming agenda for collisions against
protected care windows. When a collision is found and no open decision
already covers it, opens a Decision and runs the Conductor with a synthetic
prompt — no human in the loop until they open the app.

Calendar *writes* stay confirm-gated elsewhere; this job only challenges.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from level_core.agents.conductor import SessionInput, build_conductor
from level_core.calendar.role_collisions import (
    RoleCollision,
    find_role_collisions,
    synthesize_demo_collision_event,
)
from level_core.calendar.sync_state import build_calendar_sync_store
from level_core.calendar.usuals import (
    DEFAULT_TZ,
    UsualGap,
    find_usual_gaps,
    gap_decision_key,
    horizon_dates,
)
from level_core.config import get_settings
from level_core.gateway.router import AgentGateway
from level_core.memory.base import MemoryBank
from level_core.memory.factory import build_memory
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_core.schemas.care import CareRoleId
from level_core.schemas.decision import Decision, DecisionStatus
from level_core.schemas.turn import TurnStatus
from level_jobs.base import run_job

_logger = get_logger(__name__)


async def main() -> int:
    settings = get_settings()
    memory = build_memory(settings)
    gemini = build_gemini_client(settings)
    embedder = build_embedding_client(settings)
    conductor = build_conductor(
        memory=memory,
        gemini=gemini,
        embedder=embedder,
        settings=settings,
        gateway=AgentGateway(),
    )

    user_ids_env = os.getenv("LEVEL_JOB_USER_IDS", "")
    user_ids = [u.strip() for u in user_ids_env.split(",") if u.strip()]
    if not user_ids:
        _logger.info(
            "no_users_configured",
            note="Set LEVEL_JOB_USER_IDS as a comma-separated list.",
        )
        return 0

    allow_demo_synth = os.getenv("LEVEL_DEMO", "").lower() in {"1", "true", "yes"}

    processed = 0
    for user_id in user_ids:
        try:
            processed += await _process_user(
                user_id, memory, conductor, allow_demo_synth=allow_demo_synth
            )
        except Exception:  # noqa: BLE001
            _logger.exception("user_processing_failed", user_id=user_id)
            continue
    _logger.info("async_challenge_complete", processed_decisions=processed)
    return 0


async def _load_agenda_events(user_id: str) -> list[dict[str, str | None]]:
    """Prefer on-disk calendar sync cache; else empty."""
    try:
        store = build_calendar_sync_store()
        state = await store.get(user_id)
        if state is None or not state.events:
            return []
        return [
            {
                "id": e.id,
                "summary": e.summary,
                "start": e.start,
                "end": e.end,
                "status": e.status,
            }
            for e in state.events.values()
            if e.summary
        ]
    except Exception:  # noqa: BLE001
        _logger.info("agenda_unavailable", user_id=user_id)
        return []


def _already_challenged(decisions: list[Decision], collision: RoleCollision) -> bool:
    needle = collision.event_summary.lower()[:40]
    for d in decisions:
        if d.status is not DecisionStatus.OPEN:
            continue
        if d.origin == "async_role_theft" and d.trigger_label:
            if needle in d.trigger_label.lower():
                return True
        if d.frame and needle in (d.frame.subject or "").lower():
            return True
    return False


def _already_gapped(decisions: list[Decision], gap: UsualGap) -> bool:
    key = gap_decision_key(gap.usual_id, gap.on_date)
    for d in decisions:
        if d.status is not DecisionStatus.OPEN:
            continue
        if d.origin != "async_usual_gap":
            continue
        label = d.trigger_label or ""
        if key in label:
            return True
    return False


async def _open_usual_gap(
    *,
    user_id: str,
    gap: UsualGap,
    memory: MemoryBank,
    conductor: object,
) -> int:
    key = gap_decision_key(gap.usual_id, gap.on_date)
    decision = Decision(
        user_id=user_id,
        status=DecisionStatus.OPEN,
        opened_at=datetime.now(tz=timezone.utc),
        origin="async_usual_gap",
        trigger_label=f"{key} {gap.banner()}",
        written_by="async_challenge@v1",
    )
    await memory.decisions.create(decision)
    user_text = (
        f"{gap.banner()} "
        f"Resolve: put it back, this week is different, or not me."
    )
    manifesto = await memory.manifestos.get_current_manifesto(user_id=user_id)
    snippet = manifesto.statement[:500] if manifesto else ""
    turn = await conductor.run_turn(  # type: ignore[union-attr]
        SessionInput(
            user_id=user_id,
            decision_id=decision.decision_id,
            user_text=user_text,
            manifesto_snippet=snippet,
        )
    )
    _logger.info(
        "async_usual_gap_challenge_created",
        user_id=user_id,
        decision_id=decision.decision_id,
        turn_status=turn.status.value,
        usual_id=gap.usual_id,
        on_date=gap.on_date.isoformat(),
    )
    if turn.status in (TurnStatus.COMPLETE, TurnStatus.DEGRADED, TurnStatus.BLOCKED):
        return 1
    return 0


async def _process_user(
    user_id: str,
    memory: MemoryBank,
    conductor: object,
    *,
    allow_demo_synth: bool = False,
) -> int:
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    if care is None or (not care.roles and not care.people_profiles):
        _logger.info("async_challenge_skip_no_care_profile", user_id=user_id)
        return 0

    events = await _load_agenda_events(user_id)
    collisions = find_role_collisions(care=care, events=events) if care.roles else []
    if not collisions and allow_demo_synth:
        demo = synthesize_demo_collision_event(care)
        if demo:
            events = [demo]
            collisions = find_role_collisions(care=care, events=events)
            if not collisions:
                role = next(
                    (
                        r
                        for r in care.roles
                        if r.role_id is CareRoleId.CHILD_CARE
                        or r.protected_windows
                    ),
                    care.roles[0],
                )
                window = (
                    role.protected_windows[0].label
                    if role.protected_windows
                    else role.label
                )
                start = datetime.fromisoformat(
                    (demo.get("start") or "").replace("Z", "+00:00")
                )
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                collisions = [
                    RoleCollision(
                        event_summary=demo["summary"] or "Networking dinner",
                        event_start=start,
                        role_id=role.role_id,
                        role_label=role.label,
                        window_label=window,
                        people=tuple(role.people[:3]),
                        confirmed=True,
                    )
                ]

    existing = await memory.decisions.list_for_user(user_id=user_id, limit=40)
    opened = 0

    collision = next(
        (c for c in collisions if not _already_challenged(existing, c)), None
    )
    if collision is not None:
        trigger = collision.theft_message
        decision = Decision(
            user_id=user_id,
            status=DecisionStatus.OPEN,
            opened_at=datetime.now(tz=timezone.utc),
            origin="async_role_theft",
            trigger_label=trigger,
            written_by="async_challenge@v1",
        )
        await memory.decisions.create(decision)

        user_text = (
            f"I'm about to keep this on my calendar: {collision.event_summary} "
            f"({collision.event_start.strftime('%a %b %d %H:%M UTC')}). "
            f"Should I say yes?"
        )
        manifesto = await memory.manifestos.get_current_manifesto(user_id=user_id)
        snippet = manifesto.statement[:500] if manifesto else ""

        turn = await conductor.run_turn(  # type: ignore[union-attr]
            SessionInput(
                user_id=user_id,
                decision_id=decision.decision_id,
                user_text=user_text,
                manifesto_snippet=snippet,
            )
        )

        _logger.info(
            "async_role_theft_challenge_created",
            user_id=user_id,
            decision_id=decision.decision_id,
            turn_status=turn.status.value,
            challenge_types=[q.challenge_type for q in turn.challenger_questions],
            trigger=trigger[:160],
        )
        if turn.status in (TurnStatus.COMPLETE, TurnStatus.DEGRADED, TurnStatus.BLOCKED):
            opened += 1

    today_local = datetime.now(tz=DEFAULT_TZ).date()
    gaps = find_usual_gaps(
        care=care,
        events=events,
        on_dates=horizon_dates(start=today_local, days=7),
        tz=DEFAULT_TZ,
    )
    gap = next((g for g in gaps if not _already_gapped(existing, g)), None)
    if gap is not None:
        opened += await _open_usual_gap(
            user_id=user_id,
            gap=gap,
            memory=memory,
            conductor=conductor,
        )

    if opened == 0:
        _logger.info("async_challenge_nothing_new", user_id=user_id)
    return opened


def cli() -> None:
    """Console entrypoint (declared in ``pyproject.toml``)."""
    run_job("async_challenge", main)


if __name__ == "__main__":
    cli()


__all__ = ["cli", "main"]
