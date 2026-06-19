"""Shared telemetry queries for graph health surfaces."""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.item import Item
from app.models.relationship import ItemRelationship


def ready_tenant_relationships_query(tenant_id: str):
    """Return relationships whose endpoints are ready items in the same tenant."""
    source_item = aliased(Item)
    target_item = aliased(Item)

    return (
        select(ItemRelationship)
        .join(source_item, source_item.id == ItemRelationship.source_item_id)
        .join(target_item, target_item.id == ItemRelationship.target_item_id)
        .where(
            source_item.tenant_id == tenant_id,
            target_item.tenant_id == tenant_id,
            source_item.status == "ready",
            target_item.status == "ready",
            source_item.deleted_at.is_(None),
            target_item.deleted_at.is_(None),
        )
    )


def linked_ready_tenant_item_exists(tenant_id: str):
    """Build a correlated EXISTS for ready items connected inside one tenant graph."""
    source_item = aliased(Item)
    target_item = aliased(Item)

    return (
        select(ItemRelationship.id)
        .join(source_item, source_item.id == ItemRelationship.source_item_id)
        .join(target_item, target_item.id == ItemRelationship.target_item_id)
        .where(
            or_(
                ItemRelationship.source_item_id == Item.id,
                ItemRelationship.target_item_id == Item.id,
            ),
            source_item.tenant_id == tenant_id,
            target_item.tenant_id == tenant_id,
            source_item.status == "ready",
            target_item.status == "ready",
            source_item.deleted_at.is_(None),
            target_item.deleted_at.is_(None),
        )
        .exists()
    )


async def count_orphaned_ready_items(db: AsyncSession, tenant_id: str) -> int:
    """Count ready tenant items with no visible in-tenant graph relationship."""
    result = await db.execute(
        select(func.count())
        .select_from(Item)
        .where(
            Item.tenant_id == tenant_id,
            Item.status == "ready",
            Item.deleted_at.is_(None),
            ~linked_ready_tenant_item_exists(tenant_id),
        )
    )
    return result.scalar_one()
