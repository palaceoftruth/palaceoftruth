from __future__ import annotations

import uuid

from app.embedding_profile import EmbeddingProfile, is_default_embedding_profile
from app.models.embedding import Embedding, EmbeddingProfileVector


def embedding_record_for_profile(
    *,
    item_id: uuid.UUID,
    chunk_index: int,
    chunk_text: str,
    vector: list[float],
    profile: EmbeddingProfile,
) -> Embedding | EmbeddingProfileVector:
    if is_default_embedding_profile(profile):
        return Embedding(
            item_id=item_id,
            chunk_index=chunk_index,
            chunk_text=chunk_text,
            embedding=vector,
            embedding_half=vector,
            profile_name=profile.profile_name,
            provider=profile.provider,
            model=profile.model,
            dimensions=profile.dimensions,
        )

    if profile.dimensions not in {384, 768, 1024, 1536}:
        raise ValueError(f"unsupported embedding profile dimensions: {profile.dimensions}")

    values: dict[str, object] = {
        "item_id": item_id,
        "chunk_index": chunk_index,
        "chunk_text": chunk_text,
        "profile_name": profile.profile_name,
        "provider": profile.provider,
        "model": profile.model,
        "dimensions": profile.dimensions,
        "profile_kind": profile.profile_kind,
        "input_modality": profile.input_modality,
        "profile_metadata": {
            "enabled_by_default": profile.enabled_by_default,
            "fallback_profile_name": profile.fallback_profile_name,
            "requires_api_key": profile.requires_api_key,
        },
        f"embedding_{profile.dimensions}": vector,
        f"embedding_half_{profile.dimensions}": vector,
    }
    return EmbeddingProfileVector(**values)
