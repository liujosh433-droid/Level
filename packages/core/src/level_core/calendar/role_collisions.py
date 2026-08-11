"""Detect calendar events that crowd out confirmed care-role windows.

Used by the Continuous Action ``async_challenge`` job and by commitment-gate
copy to name care collisions explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from level_core.schemas.care import (
    CARE_ROLE_LABELS,
    CareProfile,
    CareRoleId,
    CareRoleState,
    ProtectedWindow,
)
from level_core.schemas.profile import BulletStatus


@dataclass(frozen=True, slots=True)
class RoleCollision:
    """One upcoming event that overlaps a protected care window."""

    event_summary: str
    event_start: datetime
    role_id: CareRoleId
    role_label: str
    window_label: str
    people: tuple[str, ...]
    confirmed: bool  # user marked Keep (accepted) on this role
    evidence: str | None = None

    @property
    def theft_message(self) -> str:
        who = f" ({', '.join(self.people)})" if self.people else ""
        keep = "you marked Keep on" if self.confirmed else "your care profile holds"
        return (
            f"{self.event_summary} overlaps {self.role_label}{who} — "
            f"{keep} “{self.window_label}”."
        )


def _active_roles(profile: CareProfile | None) -> list[CareRoleState]:
    if profile is None:
        return []
    return [
        r
        for r in profile.roles
        if r.status is not BulletStatus.REJECTED and r.salience >= 0.35
    ]


def _parse_start(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        text = raw.replace("Z", "+00:00")
        # Date-only
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return datetime.fromisoformat(text).replace(
                hour=12, tzinfo=timezone.utc
            )
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _window_matches(window: ProtectedWindow, when: datetime) -> bool:
    """True if ``when`` falls in the sticky window (weekday + hour band)."""
    local = when.astimezone(timezone.utc)  # windows are wall-clock-ish; UTC ok for demo
    if window.weekday is not None and local.weekday() != window.weekday:
        return False
    hour = local.hour
    start_h = window.start_hour if window.start_hour is not None else 0
    end_h = window.end_hour if window.end_hour is not None else 23
    if end_h < start_h:
        return hour >= start_h or hour <= end_h
    return start_h <= hour <= end_h


def find_role_collisions(
    *,
    care: CareProfile | None,
    events: list[dict[str, str | None]],
    now: datetime | None = None,
    horizon_days: int = 14,
) -> list[RoleCollision]:
    """Return collisions between upcoming events and protected care windows."""
    now = now or datetime.now(tz=timezone.utc)
    horizon = now + timedelta(days=horizon_days)
    roles = _active_roles(care)
    if not roles:
        return []

    hits: list[RoleCollision] = []
    for ev in events:
        summary = (ev.get("summary") or "").strip()
        if not summary:
            continue
        start = _parse_start(ev.get("start"))
        if start is None or start < now - timedelta(hours=1) or start > horizon:
            continue
        for role in roles:
            for window in role.protected_windows:
                if not _window_matches(window, start):
                    continue
                hits.append(
                    RoleCollision(
                        event_summary=summary[:120],
                        event_start=start,
                        role_id=role.role_id,
                        role_label=role.label
                        or CARE_ROLE_LABELS.get(role.role_id, role.role_id.value),
                        window_label=window.label,
                        people=tuple(role.people[:3]),
                        confirmed=role.status
                        in (BulletStatus.ACCEPTED, BulletStatus.EDITED),
                        evidence=window.evidence,
                    )
                )
    # Prefer confirmed roles; dedupe by event+role
    hits.sort(key=lambda h: (not h.confirmed, h.event_start))
    seen: set[str] = set()
    unique: list[RoleCollision] = []
    for h in hits:
        key = f"{h.event_summary}|{h.role_id.value}|{h.event_start.isoformat()}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(h)
    return unique[:8]


def synthesize_demo_collision_event(
    care: CareProfile | None,
    *,
    now: datetime | None = None,
) -> dict[str, str | None] | None:
    """Build a late 'Networking dinner' that crowds out the first care window.

    Lets the Continuous Action job demo without a live Google calendar.
    """
    now = now or datetime.now(tz=timezone.utc)
    for role in _active_roles(care):
        for window in role.protected_windows:
            if window.start_hour is None:
                continue
            # Next occurrence of that weekday at the window hour
            weekday = window.weekday if window.weekday is not None else now.weekday()
            days_ahead = (weekday - now.weekday()) % 7
            if days_ahead == 0 and now.hour >= (window.start_hour or 0):
                days_ahead = 7
            when = (now + timedelta(days=days_ahead)).replace(
                hour=window.start_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            return {
                "summary": "Networking dinner — leadership team",
                "start": when.isoformat(),
            }
    # Fallback: Thursday 17:00 next week if any child_care role exists
    for role in _active_roles(care):
        if role.role_id is CareRoleId.CHILD_CARE:
            days_ahead = (3 - now.weekday()) % 7 or 7  # Thursday
            when = (now + timedelta(days=days_ahead)).replace(
                hour=17, minute=0, second=0, microsecond=0
            )
            return {
                "summary": "Networking dinner — leadership team",
                "start": when.isoformat(),
            }
    return None


def role_theft_copy_for_conflicts(
    *,
    care: CareProfile | None,
    conflict_labels: list[str],
) -> str | None:
    """Human copy when a proposal conflicts with known care windows/people."""
    roles = _active_roles(care)
    if not roles or not conflict_labels:
        return None
    blob = " ".join(conflict_labels).lower()
    for role in roles:
        people = [p.lower() for p in role.people]
        windows = [w.label.lower() for w in role.protected_windows]
        cues = people + windows + [role.label.lower(), role.role_id.value.replace("_", " ")]
        if any(c and c in blob for c in cues):
            keep = (
                "you marked Keep"
                if role.status in (BulletStatus.ACCEPTED, BulletStatus.EDITED)
                else "your care profile holds"
            )
            who = f" ({', '.join(role.people)})" if role.people else ""
            window = (
                role.protected_windows[0].label
                if role.protected_windows
                else role.label
            )
            return (
                f"That slot crowds out {role.label}{who} — {keep} on “{window}”."
            )
    # Soft: first confirmed role with a window
    for role in roles:
        if role.status in (BulletStatus.ACCEPTED, BulletStatus.EDITED) and role.protected_windows:
            return (
                f"Care collision watch: {role.label} "
                f"(“{role.protected_windows[0].label}”) may get crowded out."
            )
    return None


__all__ = [
    "RoleCollision",
    "find_role_collisions",
    "role_theft_copy_for_conflicts",
    "synthesize_demo_collision_event",
]
