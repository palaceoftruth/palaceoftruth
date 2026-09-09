"""Promotion gates on a disposable, migrated PostgreSQL database.

Set PROMOTION_TEST_DATABASE_URL explicitly. Fixtures use unique tenant IDs and
never delete data; the database belongs to the caller's disposable test runtime.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.embedding_profile import resolve_embedding_profile
from app.models.embedding import Embedding
from app.services.item_processing import process_prebuilt_item
from app.utils.hash import compute_content_hash
from app.models.item import Item
from app.models.palace import MemoryEntry
from app.models.job import Job
from app.schemas.memory import MemoryEntryRequest, MemoryScope
from app.services.memory import accept_canonical_memory_entry
from app.services.memory_promotion import promote_memory_entry_to_shared

DATABASE_URL = os.environ.get("PROMOTION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="Requires a disposable migrated PROMOTION_TEST_DATABASE_URL")


def test_real_database_promotion_concurrency_isolation_and_changed_source():
    async def scenario():
        engine = create_async_engine(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        tenant = f"promotion-test-{uuid.uuid4()}"
        async with sessions() as db:
            accepted = await accept_canonical_memory_entry(db, signing_key=None, body=MemoryEntryRequest(
                tenant_id=tenant, title="Shared test decision", body="Use the staging queue for this project.",
                source="hermes-agent-memory-tool", created_at=datetime.now(timezone.utc),
                scope=MemoryScope(type="agent", key="iris"), tags=["scope-agent", "agent-iris", "project-test"],
                metadata={"promotion": {"source_agent_scope_key": "forged"}, "trusted": True},
            ))
            source_id = uuid.UUID(accepted.job.payload["memory_entry_id"])
            source_item = await db.get(Item, accepted.source_item_id)
            source_item.status = "ready"
            source_item.content_hash = compute_content_hash(source_item.raw_content)
            accepted.job.status = "complete"
            await db.commit()

        async def promote(**overrides):
            params = dict(entry_id=source_id, tenant_id=tenant, auth_mode="mcp_oauth",
                delegated_grant_id=None, agent_scope_key="iris", containment_mode="hermes_agent",
                allowed_scopes=["write", "write:agent", "memory:promote_shared"], signing_key=None,
                client_key="hermes-iris")
            params.update(overrides)
            async with sessions() as db:
                return await promote_memory_entry_to_shared(db, **params)

        first, second = await asyncio.gather(promote(), promote())
        assert first.job.id == second.job.id
        assert sorted([first.replayed, second.replayed]) == [False, True]
        replay = await promote()
        assert replay.replayed and replay.job.id == first.job.id
        async with sessions() as db:
            shared = (await db.execute(select(MemoryEntry).where(
                MemoryEntry.tenant_id == tenant, MemoryEntry.scope_type == "tenant_shared"))).scalars().all()
            assert len(shared) == 1
            copy = shared[0]
            assert copy.id != source_id
            assert copy.metadata_["promotion"]["source_entry_id"] == str(source_id)
            assert copy.metadata_["promotion"]["source_agent_scope_key"] == "iris"
            assert "trusted" not in copy.metadata_
            original = await db.get(MemoryEntry, source_id)
            assert original.scope_type == "agent" and original.scope_key == "iris"
            assert original.superseded_by_entry_id is None
            copy_item = await db.get(Item, copy.item_id)
            assert copy_item.raw_content == source_item.raw_content
            assert "scope-agent" not in copy_item.tags and "agent-iris" not in copy_item.tags
            assert "scope-tenant_shared" in copy_item.tags
            job = await db.get(Job, first.job.id)
            assert job.payload["admission"]["promotion_actor_client_key"] == "hermes-iris"
            # Exercise the actual worker processor, not only acceptance. The
            # shared copy intentionally has the same content as its private source.
            class LocalEmbedder:
                profile = resolve_embedding_profile()
                async def embed_texts(self, texts):
                    return [[0.1] * self.profile.dimensions for _ in texts]
            processed = await process_prebuilt_item(
                db, item=copy_item, job=job, tenant_id=tenant,
                embedder=LocalEmbedder(), llm=None, enable_ai_enrichment=False,
            )
            assert processed.status == "completed"
            assert copy_item.status == "ready" and job.status == "completed"
            assert (await db.execute(select(Embedding.id).where(Embedding.item_id == copy.item_id))).scalars().all()
            assert original.scope_type == "agent" and source_item.status == "ready"

        for override in ({"tenant_id": tenant + "-other"}, {"agent_scope_key": "vera"}, {"entry_id": uuid.uuid4()}):
            with pytest.raises(HTTPException) as error:
                await promote(**override)
            assert error.value.status_code == 404

        # A changed source must not silently replay an older, different copy.
        async with sessions() as db:
            original_item = await db.get(Item, accepted.source_item_id)
            original_item.raw_content = "The staging queue decision has changed."
            await db.commit()
        with pytest.raises(HTTPException) as error:
            await promote()
        assert error.value.status_code == 409
        await engine.dispose()
    asyncio.run(scenario())


def test_real_database_activation_preserves_credentials_and_other_grants():
    from sqlalchemy import text
    from app.api.admin import set_mcp_oauth_client_shared_memory_promotion
    from app.schemas.admin import McpOAuthClientSharedMemoryPromotionRequest

    async def scenario():
        engine = create_async_engine(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        tenant = f"promotion-activation-{uuid.uuid4()}"
        client_id = uuid.uuid4()
        async with sessions() as db:
            await db.execute(text("""
                INSERT INTO mcp_clients (id, tenant_id, client_key, display_name,
                    allowed_scopes, agent_scope_key, containment_mode, oauth_client_secret_hash,
                    allow_tenant_shared_reads)
                VALUES (:id, :tenant, 'hermes-iris', 'Test Iris',
                    '["read", "write", "write:agent"]'::jsonb, 'iris', 'hermes_agent',
                    'noncredential-fixture-hash', true)
            """), {"id": client_id, "tenant": tenant})
            await db.commit()
        for enabled in (True, True, False, False):
            async with sessions() as db:
                result = await set_mcp_oauth_client_shared_memory_promotion(
                    tenant, client_id, McpOAuthClientSharedMemoryPromotionRequest(enabled=enabled), db)
                assert result.allowed_scopes == ["read", "write", "write:agent"] + (["memory:promote_shared"] if enabled else [])
                row = (await db.execute(text("SELECT oauth_client_secret_hash, allow_tenant_shared_reads FROM mcp_clients WHERE id = :id"), {"id": client_id})).one()
                assert row.oauth_client_secret_hash == "noncredential-fixture-hash"
                assert row.allow_tenant_shared_reads is True
        async with sessions() as db:
            with pytest.raises(HTTPException) as error:
                await set_mcp_oauth_client_shared_memory_promotion(
                    tenant + "-other", client_id, McpOAuthClientSharedMemoryPromotionRequest(enabled=True), db)
            assert error.value.status_code == 404
        await engine.dispose()
    asyncio.run(scenario())
