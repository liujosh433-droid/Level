"""Care graph projection — Today / Profile read model.

No LLM. Counts and nodes come from the Care Profile + agenda cache.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from level_core.ingest.google_live import _parse_when
from level_core.profile.care_infer_llm import reconcile_exclusive_people
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
    active_care_people,
    active_care_roles,
    is_self_person,
)
from level_core.schemas.profile import BulletStatus


def agenda_fingerprint(events: list[dict[str, str | None]] | None) -> str:
    """Stable short hash of calendar titles — used to invalidate graph cache."""
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
    """Resolve role from AI ``calendar_role_by_summary`` only.

    ``allow_heuristic_fallback`` is ignored on the live path (kept for call-site
    compatibility). Use :func:`classify_calendar_event` only under
    ``LEVEL_ALLOW_HEURISTIC_CARE`` / tests.
    """
    _ = allow_heuristic_fallback
    text = summary or ""
    hints = role_by_summary or {}
    if not hints:
        return None
    key = re.sub(r"\s+", " ", text.strip().lower())
    raw = hints.get(key)
    if not raw:
        return None
    try:
        return CareRoleId(raw)
    except ValueError:
        return None


def group_events_by_care_role(
    events: list[dict[str, str | None]] | None,
    *,
    role_by_summary: dict[str, str] | None = None,
) -> dict[CareRoleId, int]:
    """Count calendar events per care-role from the AI catalog only."""
    counts: Counter[CareRoleId] = Counter()
    if not events:
        return {}
    hints = role_by_summary or {}
    for ev in events:
        role = resolve_event_care_role(
            ev.get("summary") or "",
            role_by_summary=hints or None,
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

    Uses AI ``calendar_role_by_summary`` only (no regex invent). Untagged events
    go into an ``uncategorized`` slice so a partial catalog cannot read as
    ``100% childcare``.

    Returns rows: role_id, label, color, percent, event_count, minutes.
    """
    week = filter_events_for_local_week(
        events, timezone_name=timezone_name, now=now
    )
    hints = dict(profile.calendar_role_by_summary) if profile else {}
    minutes: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    for ev in week:
        summary = str(ev.get("summary") or "")
        mins = _event_duration_minutes(ev)
        if mins <= 0:
            continue
        role = resolve_event_care_role(
            summary,
            role_by_summary=hints or None,
        )
        key = role.value if role is not None else "uncategorized"
        minutes[key] += mins
        counts[key] += 1
    total = sum(minutes.values())
    if total <= 0:
        return []

    def _meta(role_key: str) -> tuple[str, str]:
        if role_key == "uncategorized":
            return "Still classifying", "#94A3B8"
        try:
            rid = CareRoleId(role_key)
            return CARE_ROLE_LABELS[rid], CARE_ROLE_COLORS[rid]
        except ValueError:
            return role_key, "#94A3B8"

    # Known roles first (by minutes), uncategorized last.
    ordered = sorted(
        minutes.items(),
        key=lambda kv: (kv[0] == "uncategorized", -kv[1], kv[0]),
    )
    rows: list[dict[str, float | int | str]] = []
    for role_key, mins in ordered:
        pct = round(100.0 * mins / total)
        if pct < 1 and counts[role_key] > 0:
            pct = 1
        label, color = _meta(role_key)
        rows.append(
            {
                "role_id": role_key,
                "label": label,
                "color": color,
                "percent": int(pct),
                "event_count": int(counts[role_key]),
                "minutes": int(round(mins)),
            }
        )
    if rows:
        drift = 100 - sum(int(r["percent"]) for r in rows)
        if drift != 0:
            # Prefer adjusting the largest non-uncategorized slice.
            anchor = next(
                (i for i, r in enumerate(rows) if r["role_id"] != "uncategorized"),
                0,
            )
            rows[anchor]["percent"] = int(rows[anchor]["percent"]) + drift
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
    """Care graph: caregiver roots (You + helpers) → dependents / domain loads."""
    # Prefer AI people assignment already on the profile; still enforce exclusivity.
    if profile is not None:
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

    rel_map = dict(profile.person_relationships) if profile else {}
    self_names = {
        p.display_name.strip().lower()
        for p in active_care_people(profile)
        if is_self_person(p) and p.display_name.strip()
    }
    self_names.update({"you", "me", "myself"})

    def _person_rel(name: str) -> str | None:
        if not name:
            return None
        if name in rel_map and rel_map[name].strip():
            return rel_map[name].strip()[:48]
        for key, val in rel_map.items():
            if key.lower() == name.lower() and val.strip():
                return val.strip()[:48]
        return None

    center = CareGraphNode(
        id="you",
        label="You",
        kind="you",
        color=CARE_YOU_COLOR,
        role_id=None,
        shape="star",
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
                    if person.strip().lower() in self_names:
                        continue
                    nid = f"child-{person.lower()}"
                    pref = _person_rel(person)
                    _add(
                        CareGraphNode(
                            id=nid,
                            label=person[:100],
                            kind="child",
                            role_id=role.role_id.value,
                            color=color,
                            event_count=count,
                            shape="circle",
                            relationship=pref,
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
                        shape="circle",
                    ),
                    from_id="you",
                    relation="holds",
                    role_id=role.role_id,
                )
                child_node_ids.append(nid)
        elif role.role_id is CareRoleId.ELDER_CARE:
            labels = role.people[:2] or ["Elder care"]
            for person in labels:
                if role.people and person.strip().lower() in self_names:
                    continue
                nid = f"elder-{person.lower().replace(' ', '-')}"
                pref = _person_rel(person) if role.people else None
                _add(
                    CareGraphNode(
                        id=nid,
                        label=(person if role.people else role.label)[:100],
                        kind="elder",
                        role_id=role.role_id.value,
                        color=color,
                        event_count=count,
                        shape="circle",
                        relationship=pref,
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
                    shape="circle",
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
                    shape="circle",
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
                    shape="circle",
                ),
                from_id="you",
                relation="carries",
                role_id=role.role_id,
            )
        elif role.role_id is CareRoleId.PARTNER_COPARENT:
            # Co-parent is their own caregiver root (not a satellite of You).
            name = role.people[0] if role.people else "Co-parent"
            pref = _person_rel(name) if role.people else None
            nid = f"helper-{name.lower().replace(' ', '-')}"
            if nid not in seen:
                nodes.append(
                    CareGraphNode(
                        id=nid,
                        label=name[:100],
                        kind="helper",
                        hint="May share child-care load",
                        role_id=role.role_id.value,
                        color=color,
                        event_count=count,
                        shape="star",
                        relationship=pref,
                    )
                )
                seen.add(nid)
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

    # Occasional helpers (friends/neighbors): own roots; arrows to who they help.
    if profile is not None:
        help_color = CARE_ROLE_COLORS[CareRoleId.CHILD_CARE]
        helper_color = CARE_ROLE_COLORS[CareRoleId.PARTNER_COPARENT]
        for helper in profile.helpers[:4]:
            label = (helper.name or "").strip() or "Friend"
            nid = f"helper-{label.lower().replace(' ', '-')}"
            if nid not in seen:
                pref = _person_rel(label)
                nodes.append(
                    CareGraphNode(
                        id=nid,
                        label=label[:100],
                        kind="helper",
                        hint=helper.hint or "Occasionally helps with care",
                        role_id=None,
                        color=helper_color,
                        event_count=0,
                        shape="star",
                        relationship=pref,
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

    # Partition: caregiver stars are roots; circles hang under them in the UI.
    caregiver_roots = [center] + [
        n for n in nodes if (n.shape or "").lower() == "star"
    ]
    dependent_nodes = [n for n in nodes if (n.shape or "").lower() != "star"]

    return CareGraph(
        center=center,
        roots=caregiver_roots,
        nodes=dependent_nodes,
        edges=unique_edges,
        categories=categories,
    )


__all__ = [
    "agenda_fingerprint",
    "build_care_graph",
    "build_holding_summary",
    "build_week_role_load",
    "cached_care_graph",
    "filter_events_for_local_week",
    "group_events_by_care_role",
    "invalidate_care_graph_cache",
    "resolve_event_care_role",
]

