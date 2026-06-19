"""Global knowledge graph endpoint — all items as nodes and all relationships as edges."""
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_api_key
from app.database import get_db
from app.models.item import Item
from app.models.relationship import ItemRelationship
from app.schemas.relationship import GraphResponse, GraphMeta, GraphNode, GraphEdge
from app.services.graph_telemetry import ready_tenant_relationships_query

router = APIRouter(prefix="/graph", tags=["graph"], dependencies=[Depends(verify_api_key)])


@router.get("", response_model=GraphResponse)
async def get_graph(
    request: Request,
    item_id: uuid.UUID | None = None,
    include_orphans: bool = True,
    node_limit: int = Query(100, ge=1, le=200),
    edge_limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Return a bounded ready-item graph for the authenticated tenant."""
    tenant_id = request.state.tenant_id

    items_query = (
        select(Item)
        .where(Item.status == "ready", Item.tenant_id == tenant_id)
        .where(Item.deleted_at.is_(None))
        .order_by(Item.updated_at.desc(), Item.id.desc())
        .limit(node_limit)
    )
    if item_id is not None:
        items_query = items_query.where(Item.id == item_id)

    items = (await db.execute(items_query)).scalars().all()

    # Collect ready tenant item IDs so graph metadata matches the visible graph.
    tenant_item_ids = {item.id for item in items}

    rels = []
    if tenant_item_ids:
        endpoint_filter = and_(
            ItemRelationship.source_item_id.in_(tenant_item_ids),
            ItemRelationship.target_item_id.in_(tenant_item_ids),
        )
        if item_id is not None:
            endpoint_filter = or_(
                ItemRelationship.source_item_id.in_(tenant_item_ids),
                ItemRelationship.target_item_id.in_(tenant_item_ids),
            )
        relationship_query = (
            ready_tenant_relationships_query(tenant_id)
            .where(endpoint_filter)
            .order_by(ItemRelationship.confidence.desc(), ItemRelationship.id.desc())
            .limit(edge_limit)
        )
        rels = (await db.execute(relationship_query)).scalars().all()
        related_item_ids = {
            endpoint_id
            for rel in rels
            for endpoint_id in (rel.source_item_id, rel.target_item_id)
        }
        missing_item_ids = related_item_ids - tenant_item_ids
        if missing_item_ids and item_id is not None:
            related_items = (
                await db.execute(
                    select(Item)
                    .where(Item.id.in_(missing_item_ids))
                    .where(Item.tenant_id == tenant_id)
                    .where(Item.status == "ready")
                    .where(Item.deleted_at.is_(None))
                    .limit(node_limit)
                )
            ).scalars().all()
            items.extend(related_items)
            tenant_item_ids.update(item.id for item in related_items)

    connected_item_ids = {
        endpoint_id
        for rel in rels
        for endpoint_id in (rel.source_item_id, rel.target_item_id)
        if endpoint_id in tenant_item_ids
    }
    if not include_orphans:
        items = [item for item in items if item.id in connected_item_ids]

    return GraphResponse(
        nodes=[
            GraphNode(id=item.id, title=item.title, source_type=item.source_type, tags=item.tags or [])
            for item in items
        ],
        edges=[
            GraphEdge(
                source=rel.source_item_id,
                target=rel.target_item_id,
                relationship=rel.relationship,
                confidence=rel.confidence,
            )
            for rel in rels
        ],
        meta=GraphMeta(orphaned_ready_items=len(tenant_item_ids - connected_item_ids)),
    )
