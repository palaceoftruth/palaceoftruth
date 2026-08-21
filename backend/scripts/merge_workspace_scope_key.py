"""One-time admin tool: merge memory_entries and memory_scope_profiles rows
from an old scope_key onto a new one.

This exists to fix scope keys that forked into two workspaces because of a
repeated prefix, for example ``workspace/acme`` (from_scope_key) that should
really be ``acme`` (to_scope_key). Dry-run by default; pass ``--execute`` to
actually write. Both ``--from-scope-key`` and ``--to-scope-key`` are required
and must be provided explicitly for each run -- this tool is not tied to any
one scope key.

This script never issues a DELETE statement, even under --execute. If a
memory_scope_profiles row is left behind at from_scope_key with no
memory_entries pointing at it anymore, the script prints the exact DELETE
statement as text for a human to review and run by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import tenant_async_session


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Tenant id to operate on.")
    parser.add_argument(
        "--from-scope-key",
        required=True,
        help="scope_key to merge from, e.g. workspace/acme.",
    )
    parser.add_argument(
        "--to-scope-key",
        required=True,
        help="scope_key to merge into, e.g. acme.",
    )
    parser.add_argument(
        "--scope-type",
        default="workspace",
        help="scope_type both keys belong to (default: workspace).",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Report what would change without writing. This is the default; "
        "same as omitting --execute.",
    )
    parser.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="Actually write the changes described by the dry-run report.",
    )
    return parser.parse_args()


def _jsonify(value: dict[str, Any] | None) -> str:
    if value is None:
        return "none"
    return json.dumps(value, default=str, sort_keys=True)


async def _memory_entries_count(
    db: AsyncSession, *, tenant_id: str, scope_type: str, scope_key: str
) -> int:
    result = await db.execute(
        text(
            "SELECT COUNT(*) FROM memory_entries "
            "WHERE tenant_id = :tenant_id AND scope_type = :scope_type AND scope_key = :scope_key"
        ),
        {"tenant_id": tenant_id, "scope_type": scope_type, "scope_key": scope_key},
    )
    return int(result.scalar_one())


async def _scope_profile_row(
    db: AsyncSession, *, tenant_id: str, scope_type: str, scope_key: str
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            "SELECT * FROM memory_scope_profiles "
            "WHERE tenant_id = :tenant_id AND scope_type = :scope_type AND scope_key = :scope_key"
        ),
        {"tenant_id": tenant_id, "scope_type": scope_type, "scope_key": scope_key},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _report_before_state(
    db: AsyncSession, *, tenant_id: str, scope_type: str, from_key: str, to_key: str
) -> tuple[int, int, dict[str, Any] | None, dict[str, Any] | None]:
    from_entries = await _memory_entries_count(
        db, tenant_id=tenant_id, scope_type=scope_type, scope_key=from_key
    )
    to_entries = await _memory_entries_count(
        db, tenant_id=tenant_id, scope_type=scope_type, scope_key=to_key
    )
    from_profile = await _scope_profile_row(
        db, tenant_id=tenant_id, scope_type=scope_type, scope_key=from_key
    )
    to_profile = await _scope_profile_row(
        db, tenant_id=tenant_id, scope_type=scope_type, scope_key=to_key
    )
    print(f"tenant={tenant_id} scope_type={scope_type} from={from_key!r} to={to_key!r}")
    print(f"[before] memory_entries matching from-key ({from_key!r}): {from_entries}")
    print(f"[before] memory_entries matching to-key   ({to_key!r}): {to_entries}")
    print(f"[before] memory_scope_profiles row for from-key: {_jsonify(from_profile)}")
    print(f"[before] memory_scope_profiles row for to-key:   {_jsonify(to_profile)}")
    return from_entries, to_entries, from_profile, to_profile


async def _main() -> None:
    args = _parse_args()
    tenant_id = args.tenant
    scope_type = args.scope_type
    from_key = args.from_scope_key
    to_key = args.to_scope_key

    if from_key == to_key:
        raise SystemExit("--from-scope-key and --to-scope-key must be different")

    async with tenant_async_session(tenant_id) as db:
        before_from_entries, before_to_entries, from_profile, to_profile = await _report_before_state(
            db, tenant_id=tenant_id, scope_type=scope_type, from_key=from_key, to_key=to_key
        )

    if args.dry_run:
        projected_to_entries = before_to_entries + before_from_entries
        print(
            "DRY RUN: no changes were made. With --execute this would move "
            f"{before_from_entries} memory_entries row(s) from {from_key!r} to {to_key!r}, "
            f"leaving 0 at {from_key!r} and {projected_to_entries} at {to_key!r}."
        )
        if from_profile is not None and to_profile is not None:
            print(
                "DRY RUN: memory_scope_profiles rows exist for both from-key and "
                "to-key. --execute will NOT merge or delete either row; they must "
                "be reconciled by hand."
            )
        elif from_profile is not None and to_profile is None:
            print(
                f"DRY RUN: --execute would rename the memory_scope_profiles row "
                f"for {from_key!r} (id={from_profile['id']}) to scope_key={to_key!r}."
            )
        else:
            print("DRY RUN: no memory_scope_profiles rename is needed.")
        print("=" * 72)
        print("Summary (dry-run, no writes):")
        print(f"  memory_entries[{from_key!r}]: before={before_from_entries} after={before_from_entries} (unchanged)")
        print(f"  memory_entries[{to_key!r}]:   before={before_to_entries} after={before_to_entries} (unchanged)")
        return

    async with tenant_async_session(tenant_id) as db:
        update_result = await db.execute(
            text(
                "UPDATE memory_entries SET scope_key = :to_key "
                "WHERE tenant_id = :tenant_id AND scope_type = :scope_type AND scope_key = :from_key"
            ),
            {
                "tenant_id": tenant_id,
                "scope_type": scope_type,
                "from_key": from_key,
                "to_key": to_key,
            },
        )
        moved_entries = update_result.rowcount

        from_profile = await _scope_profile_row(
            db, tenant_id=tenant_id, scope_type=scope_type, scope_key=from_key
        )
        to_profile = await _scope_profile_row(
            db, tenant_id=tenant_id, scope_type=scope_type, scope_key=to_key
        )

        leftover_empty_from_profile = False
        if from_profile is not None and to_profile is not None:
            print(
                "Both memory_scope_profiles rows exist. This script will NOT "
                "merge or delete either row; reconcile the counters/metadata "
                "by hand:"
            )
            print(f"  from ({from_key!r}): {_jsonify(from_profile)}")
            print(f"  to   ({to_key!r}):   {_jsonify(to_profile)}")
            # All memory_entries at from_key were just moved to to_key above,
            # so this profile row is now orphaned. Never delete it here.
            leftover_empty_from_profile = True
        elif from_profile is not None and to_profile is None:
            await db.execute(
                text(
                    "UPDATE memory_scope_profiles SET scope_key = :to_key "
                    "WHERE tenant_id = :tenant_id AND scope_type = :scope_type AND scope_key = :from_key"
                ),
                {
                    "tenant_id": tenant_id,
                    "scope_type": scope_type,
                    "from_key": from_key,
                    "to_key": to_key,
                },
            )
            print(
                f"Renamed memory_scope_profiles row id={from_profile['id']} "
                f"from scope_key={from_key!r} to scope_key={to_key!r}."
            )
        elif from_profile is None and to_profile is not None:
            print(
                f"No memory_scope_profiles row exists for {from_key!r}; the "
                f"{to_key!r} row is left unchanged."
            )
        else:
            print("No memory_scope_profiles row exists for either key. Nothing to rename.")

        await db.commit()

        after_from_entries = await _memory_entries_count(
            db, tenant_id=tenant_id, scope_type=scope_type, scope_key=from_key
        )
        after_to_entries = await _memory_entries_count(
            db, tenant_id=tenant_id, scope_type=scope_type, scope_key=to_key
        )

    print(f"Moved {moved_entries} memory_entries row(s) from {from_key!r} to {to_key!r}.")
    print("=" * 72)
    print("Summary:")
    print(f"  memory_entries[{from_key!r}]: before={before_from_entries} after={after_from_entries}")
    print(f"  memory_entries[{to_key!r}]:   before={before_to_entries} after={after_to_entries}")

    print(
        "This script never issues a DELETE statement, under --execute or "
        "otherwise; that step is explicitly out of scope."
    )
    if leftover_empty_from_profile and after_from_entries == 0:
        print(
            "A memory_scope_profiles row for the from-key now has 0 associated "
            "memory_entries. If a human decides it is safe to remove after "
            "reconciling it against the to-key row above, the statement to run "
            "by hand is:"
        )
        print(
            "  DELETE FROM memory_scope_profiles "
            f"WHERE tenant_id = '{tenant_id}' AND scope_type = '{scope_type}' "
            f"AND scope_key = '{from_key}';"
        )


if __name__ == "__main__":
    asyncio.run(_main())
