"""Admin: live agent traces + store snapshot for the demo video.

`/traces` returns the raw audit rows (flat) AND a grouped waterfall
keyed by trace_id, so the frontend can render either a table or a
Cloud-Trace-style tree without needing a separate spans store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from level_core.agents.identity import verify as verify_identity
from level_core.agents.registry import to_dict as registry_snapshot
from level_core.config import get_settings
from level_core.storage.base import UserStore

from level_api.deps import get_user_store

router = APIRouter()


def _require_admin() -> None:
    if not get_settings().level_admin_traces_enabled:
        raise HTTPException(status_code=404, detail="disabled")


@router.get("/agents")
async def list_agents() -> dict[str, Any]:
    """Live snapshot of the Agent Registry.

    Every LLM the system talks to appears here with its safety class,
    cost tier, schema, version, and prompt hash. Compare against
    `level_core.agents.*` modules to catch drift.
    """
    _require_admin()
    return {"agents": registry_snapshot()}


@router.get("/agents/verify")
async def verify_agent_identity(token: str) -> dict[str, Any]:
    """Verify a stamped identity token from an audit row.

    Grader script can pull `model` from any audit row, split on `||`,
    and hit this endpoint. Returns `verified=False` on tamper.
    """
    _require_admin()
    identity = verify_identity(token)
    if identity is None:
        return {"verified": False}
    return {
        "verified": True,
        "name": identity.name,
        "version": identity.version,
        "prompt_hash": identity.prompt_hash,
    }


@router.get("/traces")
async def traces(
    limit: int = 50, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    """Return the last `limit` agent calls, plus a grouped view.

    Grouped view is a list of trace roots; each root has a `children`
    array (0..N) of audit rows that share the same trace_id or point at
    the root via parent_audit_id. Depth is capped to keep the payload
    predictable — Level agents don't nest > 3 today.
    """
    _require_admin()
    entries = [a.model_dump(mode="json") for a in await store.ai_audit.list()]
    entries.sort(key=lambda a: a["created_at"], reverse=True)
    trimmed = entries[:limit]

    return {"traces": trimmed, "grouped": _group_by_trace(trimmed)}


def _group_by_trace(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a shallow tree from a flat list of audit rows.

    Rule: rows sharing a `trace_id` are grouped. The "root" of each group
    is the row without a `parent_audit_id` (fallback: oldest row).
    Rows whose `parent_audit_id` isn't in the group attach to the root
    so a mis-linked parent never orphans the child on the UI.
    """
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        key = str(e.get("trace_id") or e["audit_id"])
        by_trace.setdefault(key, []).append(e)

    groups: list[dict[str, Any]] = []
    for trace_id, rows in by_trace.items():
        rows.sort(key=lambda r: r["created_at"])
        root = next(
            (r for r in rows if not r.get("parent_audit_id")),
            rows[0],
        )
        children = [r for r in rows if r["audit_id"] != root["audit_id"]]
        # Roll up aggregates so the header row shows chain totals at a glance.
        total_cost = sum(float(r.get("cost_estimate_usd") or 0) for r in rows)
        total_latency = sum(int(r.get("latency_ms") or 0) for r in rows)
        any_hallucinated = any(bool(r.get("hallucinated")) for r in rows)
        any_fallback = any(r.get("fallback_used") for r in rows)
        groups.append(
            {
                "trace_id": trace_id,
                "root": root,
                "children": children,
                "row_count": len(rows),
                "total_cost_usd": round(total_cost, 6),
                "total_latency_ms": total_latency,
                "any_hallucinated": any_hallucinated,
                "any_fallback": any_fallback,
                "started_at": root["created_at"],
            }
        )
    groups.sort(key=lambda g: g["started_at"], reverse=True)
    return groups


@router.get("/store")
async def store_snapshot(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    """Live per-user JSON the demo inspector diffs as Level writes."""
    _require_admin()
    profile = dict(await store.profile.read() or {})
    agenda = await store.agenda.list()
    agenda.sort(key=lambda e: e.time.start, reverse=True)
    chat = await store.chat_turns.list()
    chat.sort(key=lambda t: t.created_at, reverse=True)
    negatives = await store.negatives.list()
    negatives.sort(key=lambda n: n.created_at, reverse=True)

    def _event(e: Any) -> dict[str, Any]:
        return {
            "event_id": e.event_id,
            "summary": e.summary,
            "start": e.time.start.isoformat(),
            "end": e.time.end.isoformat(),
            "origin": e.origin,
            "activity_type": e.activity_type,
        }

    return {
        "user_id": store.user_id,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "profile": {
            "email": profile.get("email"),
            "tz": profile.get("tz"),
            "dismissed_missing_week": profile.get("dismissed_missing_week"),
            "resolved_missing_week": profile.get("resolved_missing_week"),
            "pending_booking": profile.get("pending_booking"),
            "pending_find": profile.get("pending_find"),
            "pending_email_pick": profile.get("pending_email_pick"),
            "pending_email_draft": profile.get("pending_email_draft"),
            "calendar_window_days_back": profile.get("calendar_window_days_back"),
            "calendar_window_days_forward": profile.get("calendar_window_days_forward"),
            "proactive_cards": profile.get("proactive_cards"),
            "media_cache": profile.get("media_cache"),
        },
        "people": [p.model_dump(mode="json") for p in await store.people.list()],
        "priorities": [p.model_dump(mode="json") for p in await store.priorities.list()],
        "usuals": [u.model_dump(mode="json") for u in await store.usuals.list()],
        "reminders": [r.model_dump(mode="json") for r in await store.reminders.list()],
        "contacts": [c.model_dump(mode="json") for c in await store.contacts.list()],
        "agenda": {
            "total": len(agenda),
            "level": sum(1 for e in agenda if e.origin == "level"),
            "recent": [_event(e) for e in agenda[:20]],
            "level_recent": [_event(e) for e in agenda if e.origin == "level"][:12],
        },
        "chat_turns": [
            {
                "turn_id": t.turn_id,
                "role": t.role,
                "text": (t.text or "")[:180],
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in chat[:8]
        ],
        "negatives": [n.model_dump(mode="json") for n in negatives[:12]],
    }
