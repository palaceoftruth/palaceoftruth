import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from app.config import settings
from app.database import async_session
from app.models.item import Item
from app.models.palace import PalaceDirtyItem, PalaceRun, PalaceTenantState, RoomMembership, SyncSource
from app.services.fact_registry import extract_temporal_facts, list_fact_registry_tenants, sweep_fact_registry_contradictions
from app.services.diary_rollups import generate_memory_diary_rollups
from app.services.memory_dreams import generate_memory_dreams, memory_dream_target_days
from app.services.wakeup_briefs import generate_wakeup_briefs
from app.services.palace import (
    create_or_get_palace_run,
    create_or_get_sync_run,
    inspect_palace_index_integrity,
    mark_item_dirty,
    mark_items_dirty,
    recompute_stale_room_tunnels,
    record_consolidation_candidate_events,
    repair_stale_room_artifacts,
    run_palace_run,
    run_sync_run,
    sync_source_has_local_file_changes,
)
from app.workers.queues import enqueue_palace_job

logger = logging.getLogger(__name__)
DIARY_ROLLUP_REPLAY_DAYS = 2
TUNNEL_RECOMPUTE_BATCH_SIZE = 50
DIRTY_ROOM_REFRESH_BATCH_SIZE = 50
PALACE_MAINTENANCE_PHASES = (
    ("dirty-rooms", "refresh_dirty_palace_rooms"),
    ("backlog", "recover_palace_backlog"),
    ("artifacts", "repair_palace_artifacts"),
    ("consolidation", "refresh_palace_consolidation_candidates"),
    ("tunnels", "recompute_palace_tunnel_strengths"),
    ("wakeup-briefs", "refresh_caught_up_wakeup_briefs"),
)
PALACE_SYNC_WATCHER_MIN_PROBE_SECONDS = 1


def _diary_rollup_target_days(*, today: date | None = None, replay_days: int = DIARY_ROLLUP_REPLAY_DAYS) -> tuple[date, ...]:
    reference_day = today or datetime.now(timezone.utc).date()
    if replay_days < 1:
        return ()
    # Replay a small completed-day window so late-arriving notes are folded in
    # without turning the maintenance sweep into an unbounded backfill.
    return tuple(reference_day - timedelta(days=offset) for offset in range(replay_days, 0, -1))


async def _list_diary_rollup_tenants(db) -> tuple[str, ...]:
    tenant_ids = (
        await db.execute(
            select(Item.tenant_id)
            .where(Item.source_type == "note")
            .distinct()
            .order_by(Item.tenant_id.asc())
        )
    ).scalars().all()
    return tuple(tenant_ids)


async def run_fact_registry_extraction(ctx: dict, **_ignored_future_kwargs) -> None:
    async with async_session() as db:
        tenant_ids = await list_fact_registry_tenants(db)
        if not tenant_ids:
            logger.debug("run_fact_registry_extraction skipped; no ready-item tenants found")
            return

        for tenant_id in tenant_ids:
            result = await extract_temporal_facts(db, tenant_id=tenant_id)
            logger.info(
                "run_fact_registry_extraction tenant=%s items_scanned=%d created=%d updated=%d unchanged=%d superseded=%d",
                tenant_id,
                result.items_scanned,
                result.created,
                result.updated,
                result.unchanged,
                result.superseded,
            )


async def run_wakeup_story_refresh(ctx: dict, **_ignored_future_kwargs) -> None:
    async with async_session() as db:
        tenant_ids = await list_fact_registry_tenants(db)
        if not tenant_ids:
            logger.debug("run_wakeup_story_refresh skipped; no ready-item tenants found")
            return

        for tenant_id in tenant_ids:
            result = await generate_wakeup_briefs(
                db,
                tenant_id=tenant_id,
                embedder=ctx["embedder"],
                llm=ctx["llm"],
            )
            logger.info(
                "run_wakeup_story_refresh tenant=%s created=%d updated=%d unchanged=%d deactivated=%d",
                tenant_id,
                result.created,
                result.updated,
                result.unchanged,
                result.deactivated,
            )


async def run_memory_dream_refresh(ctx: dict, **_ignored_future_kwargs) -> None:
    target_days = memory_dream_target_days()
    if not target_days:
        return

    async with async_session() as db:
        tenant_ids = await list_fact_registry_tenants(db)
        if not tenant_ids:
            logger.debug("run_memory_dream_refresh skipped; no ready-item tenants found")
            return

        for tenant_id in tenant_ids:
            for target_day in target_days:
                try:
                    result = await generate_memory_dreams(
                        db,
                        tenant_id=tenant_id,
                        embedder=ctx["embedder"],
                        llm=ctx["llm"],
                        target_day=target_day,
                    )
                    logger.info(
                        "run_memory_dream_refresh tenant=%s day=%s created=%d updated=%d unchanged=%d deactivated=%d",
                        tenant_id,
                        target_day.isoformat(),
                        result.created,
                        result.updated,
                        result.unchanged,
                        result.deactivated,
                    )
                except Exception:
                    logger.exception(
                        "run_memory_dream_refresh failed tenant=%s day=%s",
                        tenant_id,
                        target_day.isoformat(),
                    )


async def run_fact_registry_contradiction_sweep(ctx: dict, **_ignored_future_kwargs) -> None:
    async with async_session() as db:
        tenant_ids = await list_fact_registry_tenants(db)
        if not tenant_ids:
            logger.debug("run_fact_registry_contradiction_sweep skipped; no ready-item tenants found")
            return

        for tenant_id in tenant_ids:
            result = await sweep_fact_registry_contradictions(db, tenant_id=tenant_id)
            logger.info(
                "run_fact_registry_contradiction_sweep tenant=%s facts_scanned=%d contradictions=%d facts_flagged=%d facts_cleared=%d",
                tenant_id,
                result.facts_scanned,
                result.contradictions,
                result.facts_flagged,
                result.facts_cleared,
            )


async def _enqueue_missing_embedding_repairs(
    ctx: dict,
    db,
    *,
    tenant_id: str,
    item_ids: tuple[uuid.UUID, ...],
) -> tuple[uuid.UUID, ...]:
    repaired: list[uuid.UUID] = []

    for item_id in item_ids:
        item = await db.get(Item, item_id)
        if item is None or item.tenant_id != tenant_id or item.status != "ready" or not item.raw_content:
            continue

        # Flip to processing before enqueue so a later sweep does not duplicate the repair.
        item.status = "processing"
        await db.commit()
        try:
            await ctx["redis"].enqueue_job(
                "embed_item",
                item_id=str(item.id),
                skip_ai_enrichment=False,
                tenant_id=tenant_id,
            )
        except Exception:
            item.status = "ready"
            await db.commit()
            raise
        repaired.append(item.id)

    return tuple(repaired)


async def _enqueue_follow_on_palace_run(
    ctx: dict,
    db,
    *,
    tenant_id: str,
    triggered_by: str,
) -> bool:
    state = await db.get(PalaceTenantState, tenant_id)
    if state is None or state.dirty_generation <= state.indexed_generation:
        return False

    palace_run, created = await create_or_get_palace_run(
        db,
        tenant_id=tenant_id,
        triggered_by=triggered_by,
    )
    if created:
        logger.info(
            "queued follow-on palace run for tenant %s trigger=%s run_id=%s at generation %s",
            tenant_id,
            triggered_by,
            palace_run.id,
            palace_run.requested_generation,
        )
        await enqueue_palace_job(ctx["redis"], "palace_run_build", palace_run_id=str(palace_run.id))
    else:
        logger.info(
            "follow-on palace run coalesced for tenant %s trigger=%s active_run_id=%s status=%s generation=%s",
            tenant_id,
            triggered_by,
            palace_run.id,
            getattr(palace_run, "status", "unknown"),
            palace_run.requested_generation,
        )
    return True


async def _refresh_wakeup_briefs_for_caught_up_palace(
    ctx: dict,
    db,
    *,
    tenant_id: str,
) -> None:
    state = await db.get(PalaceTenantState, tenant_id)
    if (
        state is None
        or state.dirty_generation > state.indexed_generation
        or getattr(state, "active_palace_run_id", None) is not None
    ):
        return

    embedder = ctx.get("embedder")
    llm = ctx.get("llm")
    if embedder is None or llm is None:
        logger.warning("skipped wake-up brief refresh for tenant %s; worker services missing", tenant_id)
        return

    try:
        result = await generate_wakeup_briefs(
            db,
            tenant_id=tenant_id,
            embedder=embedder,
            llm=llm,
        )
        logger.info(
            "refreshed wake-up briefs after Palace build tenant=%s created=%d updated=%d unchanged=%d deactivated=%d",
            tenant_id,
            result.created,
            result.updated,
            result.unchanged,
            result.deactivated,
        )
    except Exception:
        logger.exception("wake-up brief refresh after Palace build failed for tenant %s", tenant_id)


async def palace_run_build(ctx: dict, palace_run_id: str, **_ignored_future_kwargs) -> None:
    logger.info("palace_run_build worker executing run_id=%s", palace_run_id)
    async with async_session() as db:
        status, _error = await run_palace_run(db, run_id=uuid.UUID(palace_run_id))
        logger.info("palace_run_build worker finished run_id=%s status=%s", palace_run_id, status)
        if status == "completed":
            run = await db.get(PalaceRun, uuid.UUID(palace_run_id))
            if run is not None:
                has_follow_on = await _enqueue_follow_on_palace_run(
                    ctx,
                    db,
                    tenant_id=run.tenant_id,
                    triggered_by="auto",
                )
                if not has_follow_on:
                    await _refresh_wakeup_briefs_for_caught_up_palace(
                        ctx,
                        db,
                        tenant_id=run.tenant_id,
                    )


async def run_sync_source(ctx: dict, sync_run_id: str, **_ignored_future_kwargs) -> None:
    async with async_session() as db:
        status, _error = await run_sync_run(
            db,
            run_id=uuid.UUID(sync_run_id),
            embedder=ctx["embedder"],
            llm=ctx["llm"],
        )
        if status == "completed":
            # reload via explicit query, keeping tenant/source context in the same session
            from app.models.palace import SyncRun

            sync_run = await db.get(SyncRun, uuid.UUID(sync_run_id))
            if sync_run and sync_run.generation > 0:
                palace_run, created = await create_or_get_palace_run(
                    db,
                    tenant_id=sync_run.tenant_id,
                    triggered_by="sync",
                    source_sync_run_id=sync_run.id,
                )
                if created:
                    await enqueue_palace_job(ctx["redis"], "palace_run_build", palace_run_id=str(palace_run.id))


async def _enqueue_sync_source_run(ctx: dict, db, *, source: SyncSource, triggered_by: str) -> bool:
    sync_run, created = await create_or_get_sync_run(
        db,
        tenant_id=source.tenant_id,
        source=source,
        triggered_by=triggered_by,
    )
    if created:
        await enqueue_palace_job(ctx["redis"], "run_sync_source", sync_run_id=str(sync_run.id))
    return created


async def poll_sync_sources(ctx: dict) -> None:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(SyncSource)
                .where(SyncSource.status == "active")
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)
        for source in rows:
            watcher_detected_change = await sync_source_has_local_file_changes(db, source)
            if source.last_synced_at is not None:
                age = (now - source.last_synced_at).total_seconds()
                if age < source.scan_interval_seconds and not watcher_detected_change:
                    continue
            await _enqueue_sync_source_run(
                ctx,
                db,
                source=source,
                triggered_by="watcher" if watcher_detected_change else "scheduled",
            )


async def watch_local_sync_sources_once(ctx: dict) -> int:
    """Probe local sync sources between scheduled rescans and enqueue changed ones."""
    enqueued = 0
    async with async_session() as db:
        rows = (
            await db.execute(
                select(SyncSource)
                .where(SyncSource.status == "active")
                .where(SyncSource.source_kind.in_(("folder", "repo")))
            )
        ).scalars().all()

        for source in rows:
            try:
                if not await sync_source_has_local_file_changes(db, source):
                    continue
                if await _enqueue_sync_source_run(ctx, db, source=source, triggered_by="watcher"):
                    enqueued += 1
            except Exception:
                logger.exception(
                    "local sync watcher failed for source_id=%s tenant=%s",
                    source.id,
                    source.tenant_id,
                )
    return enqueued


async def watch_local_sync_sources(ctx: dict) -> None:
    if not settings.palace_sync_watcher_enabled:
        logger.info("Palace sync watcher disabled")
        return

    interval_seconds = max(
        int(settings.palace_sync_watcher_probe_seconds),
        PALACE_SYNC_WATCHER_MIN_PROBE_SECONDS,
    )
    logger.info("Palace sync watcher enabled; probe interval=%ss", interval_seconds)

    while True:
        try:
            enqueued = await watch_local_sync_sources_once(ctx)
            if enqueued:
                logger.info("Palace sync watcher enqueued %d sync run(s)", enqueued)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Palace sync watcher probe failed")
        await asyncio.sleep(interval_seconds)


async def run_diary_rollup_maintenance(ctx: dict, **_ignored_future_kwargs) -> None:
    target_days = _diary_rollup_target_days()
    if not target_days:
        return

    async with async_session() as db:
        tenant_ids = await _list_diary_rollup_tenants(db)
        if not tenant_ids:
            logger.debug("run_diary_rollup_maintenance skipped; no note tenants found")
            return

        for tenant_id in tenant_ids:
            for target_day in target_days:
                result = await generate_memory_diary_rollups(
                    db,
                    tenant_id=tenant_id,
                    embedder=ctx["embedder"],
                    llm=ctx["llm"],
                    target_day=target_day,
                )
                logger.info(
                    "run_diary_rollup_maintenance tenant=%s day=%s created=%d updated=%d unchanged=%d deactivated=%d",
                    tenant_id,
                    target_day.isoformat(),
                    result.created,
                    result.updated,
                    result.unchanged,
                    result.deactivated,
                )


async def recover_palace_backlog(ctx: dict) -> None:
    async with async_session() as db:
        states = (
            await db.execute(
                select(PalaceTenantState)
                .where(PalaceTenantState.dirty_generation > PalaceTenantState.indexed_generation)
                .order_by(PalaceTenantState.updated_at.asc())
            )
        ).scalars().all()

        for state in states:
            await _enqueue_follow_on_palace_run(
                ctx,
                db,
                tenant_id=state.tenant_id,
                triggered_by="maintenance",
            )


async def refresh_dirty_palace_rooms(ctx: dict) -> None:
    async with async_session() as db:
        states = (
            await db.execute(
                select(PalaceTenantState)
                .where(PalaceTenantState.indexed_generation > 0)
                .where(PalaceTenantState.active_palace_run_id.is_(None))
                .order_by(PalaceTenantState.updated_at.asc())
            )
        ).scalars().all()

        for state in states:
            item_ids = (
                await db.execute(
                    select(Item.id)
                    .outerjoin(
                        RoomMembership,
                        and_(
                            RoomMembership.item_id == Item.id,
                            RoomMembership.tenant_id == state.tenant_id,
                        ),
                    )
                    .outerjoin(
                        PalaceDirtyItem,
                        and_(
                            PalaceDirtyItem.item_id == Item.id,
                            PalaceDirtyItem.tenant_id == state.tenant_id,
                        ),
                    )
                    .where(Item.tenant_id == state.tenant_id)
                    .where(Item.status == "ready")
                    .where(Item.deleted_at.is_(None))
                    # Derived artifacts are not source memories that need room
                    # membership repair.
                    .where(~Item.metadata_.has_key("wakeup_brief"))
                    .where(~Item.metadata_.has_key("memory_dream"))
                    .group_by(Item.id)
                    .having(func.count(RoomMembership.id) == 0)
                    .having(func.count(PalaceDirtyItem.id) == 0)
                    .order_by(Item.updated_at.asc(), Item.id.asc())
                    .limit(DIRTY_ROOM_REFRESH_BATCH_SIZE)
                )
            ).scalars().all()
            if not item_ids:
                continue

            for item_id in item_ids:
                await mark_item_dirty(
                    db,
                    tenant_id=state.tenant_id,
                    item_id=item_id,
                    reason="maintenance",
                )

            palace_run, created = await create_or_get_palace_run(
                db,
                tenant_id=state.tenant_id,
                triggered_by="maintenance",
            )
            if created:
                await enqueue_palace_job(ctx["redis"], "palace_run_build", palace_run_id=str(palace_run.id))
            logger.info(
                "refresh_dirty_palace_rooms tenant=%s items=%d generation=%d",
                state.tenant_id,
                len(item_ids),
                palace_run.requested_generation,
            )


async def run_palace_maintenance(ctx: dict, **_ignored_future_kwargs) -> None:
    """Run the bounded Palace upkeep phases without letting one failure starve the rest."""
    for phase_name, task_name in PALACE_MAINTENANCE_PHASES:
        task = globals()[task_name]
        try:
            await task(ctx)
        except Exception:
            logger.exception("run_palace_maintenance phase=%s failed", phase_name)


async def repair_palace_artifacts(ctx: dict) -> None:
    async with async_session() as db:
        states = (
            await db.execute(
                select(PalaceTenantState)
                .where(PalaceTenantState.indexed_generation > 0)
                .where(PalaceTenantState.active_palace_run_id.is_(None))
                .where(PalaceTenantState.dirty_generation <= PalaceTenantState.indexed_generation)
                .order_by(PalaceTenantState.updated_at.asc())
            )
        ).scalars().all()

        for state in states:
            repair_plan = await repair_stale_room_artifacts(
                db,
                tenant_id=state.tenant_id,
                target_generation=state.indexed_generation,
            )
            repaired_count = (
                len(repair_plan.closet_room_ids)
                + len(repair_plan.snapshot_room_ids)
                + len(repair_plan.tunnel_room_ids)
            )
            if repaired_count or repair_plan.blocked_room_ids:
                logger.info(
                    "repair_palace_artifacts tenant=%s closets=%d snapshots=%d tunnels=%d blocked=%d generation=%d",
                    state.tenant_id,
                    len(repair_plan.closet_room_ids),
                    len(repair_plan.snapshot_room_ids),
                    len(repair_plan.tunnel_room_ids),
                    len(repair_plan.blocked_room_ids),
                    state.indexed_generation,
                )


async def refresh_palace_consolidation_candidates(ctx: dict, **_ignored_future_kwargs) -> None:
    async with async_session() as db:
        states = (
            await db.execute(
                select(PalaceTenantState)
                .where(PalaceTenantState.indexed_generation > 0)
                .where(PalaceTenantState.active_palace_run_id.is_(None))
                .order_by(PalaceTenantState.updated_at.asc())
            )
        ).scalars().all()

        for state in states:
            summary = await record_consolidation_candidate_events(
                db,
                tenant_id=state.tenant_id,
            )
            if summary.candidate_count:
                logger.info(
                    "refresh_palace_consolidation_candidates tenant=%s candidates=%d surfaced=%d",
                    state.tenant_id,
                    summary.candidate_count,
                    len(summary.candidates),
                )


async def recompute_palace_tunnel_strengths(ctx: dict, **_ignored_future_kwargs) -> None:
    async with async_session() as db:
        states = (
            await db.execute(
                select(PalaceTenantState)
                .where(PalaceTenantState.indexed_generation > 0)
                .where(PalaceTenantState.active_palace_run_id.is_(None))
                .where(PalaceTenantState.dirty_generation <= PalaceTenantState.indexed_generation)
                .order_by(PalaceTenantState.updated_at.asc())
            )
        ).scalars().all()

        for state in states:
            result = await recompute_stale_room_tunnels(
                db,
                tenant_id=state.tenant_id,
                target_generation=state.indexed_generation,
                limit=TUNNEL_RECOMPUTE_BATCH_SIZE,
            )
            if result.room_ids:
                logger.info(
                    "recompute_palace_tunnel_strengths tenant=%s rooms=%d generation=%d",
                    state.tenant_id,
                    len(result.room_ids),
                    result.target_generation,
                )


async def refresh_caught_up_wakeup_briefs(ctx: dict, **_ignored_future_kwargs) -> None:
    async with async_session() as db:
        states = (
            await db.execute(
                select(PalaceTenantState)
                .where(PalaceTenantState.indexed_generation > 0)
                .where(PalaceTenantState.active_palace_run_id.is_(None))
                .where(PalaceTenantState.dirty_generation <= PalaceTenantState.indexed_generation)
                .order_by(PalaceTenantState.updated_at.asc())
            )
        ).scalars().all()

        for state in states:
            await _refresh_wakeup_briefs_for_caught_up_palace(
                ctx,
                db,
                tenant_id=state.tenant_id,
            )


async def sweep_palace_index_integrity(ctx: dict) -> None:
    async with async_session() as db:
        states = (
            await db.execute(
                select(PalaceTenantState)
                .where(PalaceTenantState.indexed_generation > 0)
                .order_by(PalaceTenantState.updated_at.asc())
            )
        ).scalars().all()

        for state in states:
            integrity_plan = await inspect_palace_index_integrity(
                db,
                tenant_id=state.tenant_id,
                target_generation=state.indexed_generation,
            )
            membership_repairs = 0
            for item_id in integrity_plan.missing_membership_item_ids:
                item = await db.get(Item, item_id)
                if item is None or item.tenant_id != state.tenant_id or item.status != "ready":
                    continue
                await mark_item_dirty(
                    db,
                    tenant_id=state.tenant_id,
                    item_id=item.id,
                    reason="integrity-sweep",
                )
                membership_repairs += 1

            if membership_repairs:
                palace_run, created = await create_or_get_palace_run(
                    db,
                    tenant_id=state.tenant_id,
                    triggered_by="maintenance",
                )
                if created:
                    await enqueue_palace_job(ctx["redis"], "palace_run_build", palace_run_id=str(palace_run.id))

            repaired_embeddings = await _enqueue_missing_embedding_repairs(
                ctx,
                db,
                tenant_id=state.tenant_id,
                item_ids=integrity_plan.missing_embedding_item_ids,
            )

            artifact_plan = integrity_plan.artifact_repair_plan
            has_artifact_repairs = (
                bool(artifact_plan.closet_room_ids)
                or bool(artifact_plan.snapshot_room_ids)
                or bool(artifact_plan.tunnel_room_ids)
            )
            if has_artifact_repairs:
                await repair_stale_room_artifacts(
                    db,
                    tenant_id=state.tenant_id,
                    target_generation=state.indexed_generation,
                )

            if repaired_embeddings or membership_repairs or has_artifact_repairs or artifact_plan.blocked_room_ids:
                logger.info(
                    "sweep_palace_index_integrity tenant=%s embeddings=%d memberships=%d closets=%d snapshots=%d tunnels=%d blocked=%d generation=%d",
                    state.tenant_id,
                    len(repaired_embeddings),
                    membership_repairs,
                    len(artifact_plan.closet_room_ids),
                    len(artifact_plan.snapshot_room_ids),
                    len(artifact_plan.tunnel_room_ids),
                    len(artifact_plan.blocked_room_ids),
                    state.indexed_generation,
                )


async def mark_item_dirty_and_schedule(ctx: dict, item_id: str, tenant_id: str = "default", reason: str = "ingest") -> None:
    await mark_items_dirty_and_schedule(ctx, item_ids=[item_id], tenant_id=tenant_id, reason=reason)


async def mark_items_dirty_and_schedule(
    ctx: dict,
    item_ids: list[str],
    tenant_id: str = "default",
    reason: str = "ingest",
) -> None:
    try:
        parsed_item_ids = tuple(dict.fromkeys(uuid.UUID(item_id) for item_id in item_ids))
    except (TypeError, ValueError) as exc:
        logger.warning("mark_items_dirty_and_schedule skipped invalid item_ids: %s", exc)
        return
    if not parsed_item_ids:
        return

    async with async_session() as db:
        existing_item_ids = (
            await db.execute(
                select(Item.id)
                .where(Item.tenant_id == tenant_id)
                .where(Item.id.in_(parsed_item_ids))
            )
        ).scalars().all()
        if not existing_item_ids:
            return
        await mark_items_dirty(
            db,
            tenant_id=tenant_id,
            item_ids=existing_item_ids,
            reason=reason,
        )
        palace_run, created = await create_or_get_palace_run(
            db,
            tenant_id=tenant_id,
            triggered_by="auto",
        )
        await db.commit()
        if created:
            await enqueue_palace_job(ctx["redis"], "palace_run_build", palace_run_id=str(palace_run.id))
