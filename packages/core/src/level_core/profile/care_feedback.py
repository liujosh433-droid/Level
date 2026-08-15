"""Keep / Not me on Care Profile role bullets."""

from __future__ import annotations

from datetime import datetime, timezone

from level_core.schemas.care import CareProfile, CareRoleState
from level_core.schemas.profile import BulletStatus, ProfileSnapshot


def merge_role_feedback(
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


__all__ = [
    "apply_bullet_feedback_to_care_profile",
    "merge_role_feedback",
]
