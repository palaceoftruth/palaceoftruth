import uuid

from pydantic import BaseModel, Field


class RelatedItemResponse(BaseModel):
    item_id: uuid.UUID
    title: str
    source_type: str
    relationship: str
    confidence: float

    model_config = {"from_attributes": True}


class RelatedItemsResponse(BaseModel):
    relationships: list[RelatedItemResponse]


class GraphNode(BaseModel):
    id: uuid.UUID
    title: str
    source_type: str
    tags: list[str] = []


class GraphEdge(BaseModel):
    source: uuid.UUID
    target: uuid.UUID
    relationship: str
    confidence: float


class GraphMeta(BaseModel):
    orphaned_ready_items: int = 0


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    meta: GraphMeta = Field(default_factory=GraphMeta)
