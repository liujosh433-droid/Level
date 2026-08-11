#!/usr/bin/env python3
"""Judge-facing Continuous Action demo (no UI required).

Runs: ingest fixtures → Care Profile mutation → async role-theft challenge
→ retention prune. Prints proof lines for the video / README.

Usage:
    LEVEL_ENV=local .venv/bin/python scripts/demo_continuous_action.py
    # or: make demo-judge
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone


async def main() -> int:
    root = os.path.join(os.path.dirname(__file__), "..")
    sys.path.insert(0, os.path.join(root, "packages", "core", "src"))
    sys.path.insert(0, os.path.join(root, "packages", "jobs", "src"))

    os.environ.setdefault("LEVEL_ENV", "local")
    os.environ.setdefault("LEVEL_INGEST_FIXTURES", "1")
    user_id = os.getenv("LEVEL_JOB_USER_IDS", "demo-parent").split(",")[0].strip()
    os.environ["LEVEL_JOB_USER_IDS"] = user_id

    from level_core.config import get_settings
    from level_core.memory.factory import build_memory
    from level_core.memory.retention import RetentionPolicy, prune_user_facts
    from level_core.schemas.profile import BulletStatus
    from level_jobs import async_challenge, ingest_all

    settings = get_settings()
    memory = build_memory(settings)

    print("=== Level Continuous Action demo ===")
    print(f"user={user_id} env={settings.env.value}")
    print()

    print("1) Ingest messy fixture signals + mutate Care Profile")
    await ingest_all.main()
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    facts = await memory.facts.list_for_user(user_id=user_id, limit=500)
    if care is None or not care.roles:
        print("   FAIL: Care Profile empty after ingest")
        return 1
    print(f"   Care Profile v{care.version} · {len(care.roles)} roles · {len(facts)} facts")
    for role in sorted(care.roles, key=lambda r: r.salience, reverse=True):
        windows = ", ".join(w.label for w in role.protected_windows[:2]) or "—"
        print(f"   - {role.label} (salience={role.salience:.2f}) windows: {windows}")
    if care.conflict_summaries:
        print(f"   conflicts: {care.conflict_summaries[0]}")

    # Mark top care role Keep so async_challenge / judges see confirmed protection.
    top = max(care.roles, key=lambda r: r.salience)
    if top.status is not BulletStatus.ACCEPTED:
        roles = []
        for r in care.roles:
            if r.role_id == top.role_id:
                roles.append(r.model_copy(update={"status": BulletStatus.ACCEPTED}))
            else:
                roles.append(r)
        care = care.model_copy(
            update={
                "roles": roles,
                "version": care.version + 1,
                "updated_at": datetime.now(tz=timezone.utc),
            }
        )
        await memory.manifestos.save_care_profile(care)
        print(f"   Keep'd {top.label} → Care Profile v{care.version}")

    print()
    print("2) Background job: async role-theft challenge (no human prompt)")
    before = await memory.decisions.list_for_user(user_id=user_id, limit=50)
    before_ids = {d.decision_id for d in before}
    n = await async_challenge.main()
    after = await memory.decisions.list_for_user(user_id=user_id, limit=50)
    created = [d for d in after if d.decision_id not in before_ids]
    theft = [d for d in created if d.origin == "async_role_theft"]
    print(f"   job_return={n} new_decisions={len(created)} role_theft={len(theft)}")
    if theft:
        d = theft[0]
        print(f"   decision={d.decision_id}")
        print(f"   trigger={d.trigger_label}")
        turns = await memory.decisions.list_turns(
            user_id=user_id, decision_id=d.decision_id
        )
        for t in turns:
            if t.challenger_questions:
                q = t.challenger_questions[0]
                print(f"   challenge_type={q.challenge_type}")
                print(f"   question={q.question[:200]}")
                break
    else:
        print("   (no new collision — may already exist; check Today pending banner)")

    print()
    print("3) Retention prune (TTL + soft cap; protect Keep'd pins)")
    before_n = len(await memory.facts.list_for_user(user_id=user_id, limit=2000))
    # Force a pruneable stale event for demo visibility
    from level_core.schemas.signal import Fact, FactType
    from datetime import timedelta

    stale = Fact(
        user_id=user_id,
        type=FactType.EVENT,
        statement="Old one-off calendar noise — safe to prune",
        salience=0.2,
        created_at=datetime.now(tz=timezone.utc) - timedelta(days=200),
        updated_at=datetime.now(tz=timezone.utc) - timedelta(days=200),
    )
    await memory.facts.upsert(stale)
    result = await prune_user_facts(
        memory,
        user_id=user_id,
        policy=RetentionPolicy(max_facts_per_user=150, event_ttl_days=90),
    )
    after_n = len(await memory.facts.list_for_user(user_id=user_id, limit=2000))
    print(
        f"   facts {before_n + 1} → {after_n} (pruned={result.pruned}, "
        f"protected={result.protected})"
    )
    care2 = await memory.manifestos.get_care_profile(user_id=user_id)
    print(
        f"   Care Profile still v{care2.version if care2 else '?'} "
        f"with {len(care2.roles) if care2 else 0} roles (not TTL'd)"
    )

    print()
    print("=== Demo complete — show Today 'Care collision' banner + Profile vN in UI ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
