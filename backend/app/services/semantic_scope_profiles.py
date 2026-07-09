from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.memory import (
    MemoryScope,
    MemoryScopeListResponse,
    MemoryScopeProfile,
    MemoryScopeProfileUpsertRequest,
)
from app.services.memory import (
    get_memory_scope_profile,
    list_memory_scopes,
    upsert_memory_scope_profile,
)


class SemanticScopeProfileService:
    """Semantic-memory facade for the Palace-owned scope profile store."""

    def __init__(self, db: AsyncSession, *, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id

    async def get_profile(self, scope: MemoryScope) -> MemoryScopeProfile:
        return await get_memory_scope_profile(self.db, tenant_id=self.tenant_id, scope=scope)

    async def upsert_profile(self, body: MemoryScopeProfileUpsertRequest) -> MemoryScopeProfile:
        return await upsert_memory_scope_profile(self.db, tenant_id=self.tenant_id, body=body)

    async def list_profiles(self, *, limit: int = 50, sample_limit: int = 8) -> MemoryScopeListResponse:
        return await list_memory_scopes(
            self.db,
            tenant_id=self.tenant_id,
            limit=limit,
            sample_limit=sample_limit,
        )
