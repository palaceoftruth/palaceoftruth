#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import delete, or_, select

from app.database import async_session
from app.embedding_profile import is_default_embedding_profile, resolve_embedding_profile
from app.models.embedding import Embedding, EmbeddingProfileVector
from app.models.item import Item
from app.services.chunker import chunk_text
from app.services.embedder import EmbeddingService
from app.services.embedding_storage import embedding_record_for_profile


async def reembed_tenant(*, tenant_id: str, limit: int | None) -> None:
    embedder = EmbeddingService()

    async with async_session() as db:
        query = (
            select(Item.id)
            .where(Item.tenant_id == tenant_id)
            .where(Item.status != "failed")
            .where(or_(Item.raw_content.is_not(None), Item.content_chunks.is_not(None)))
            .order_by(Item.created_at.asc())
        )
        if limit is not None:
            query = query.limit(limit)
        item_ids = list((await db.execute(query)).scalars().all())

    total = len(item_ids)
    print(f"re-embedding tenant={tenant_id} items={total}")

    if total == 0:
        return

    async with async_session() as db:
        for index, item_id in enumerate(item_ids, start=1):
            item = await db.get(Item, item_id)
            if item is None:
                continue

            chunks = item.content_chunks or (chunk_text(item.raw_content) if item.raw_content else [])
            if not chunks:
                continue
            vectors = await embedder.embed_texts([chunk["text"] for chunk in chunks]) if chunks else []

            embedding_profile = getattr(embedder, "profile", resolve_embedding_profile())
            if is_default_embedding_profile(embedding_profile):
                await db.execute(delete(Embedding).where(Embedding.item_id == item_id))
            else:
                await db.execute(
                    delete(EmbeddingProfileVector)
                    .where(EmbeddingProfileVector.item_id == item_id)
                    .where(EmbeddingProfileVector.profile_name == embedding_profile.profile_name)
                )
            item.content_chunks = chunks
            item.status = "ready"

            for chunk_index, (chunk, vector) in enumerate(zip(chunks, vectors)):
                db.add(
                    embedding_record_for_profile(
                        item_id=uuid.UUID(str(item_id)),
                        chunk_index=chunk.get("index", chunk_index),
                        chunk_text=chunk["text"],
                        vector=vector,
                        profile=embedding_profile,
                    )
                )

            await db.commit()
            print(f"[{index}/{total}] re-embedded {item_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-embed all items for a tenant with the current embedding model.")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(reembed_tenant(tenant_id=args.tenant_id, limit=args.limit))


if __name__ == "__main__":
    main()
