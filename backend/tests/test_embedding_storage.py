import uuid

from app.embedding_profile import resolve_embedding_profile
from app.models.embedding import EmbeddingProfileVector
from app.services.embedding_storage import embedding_record_for_profile


def test_embedding_profile_vector_stores_native_profile_metadata_separately() -> None:
    profile = resolve_embedding_profile(
        profile_name="local-http-clip-native-image-768",
        experimental_profiles_enabled=True,
    )

    record = embedding_record_for_profile(
        item_id=uuid.uuid4(),
        chunk_index=0,
        chunk_text="native image vector placeholder",
        vector=[0.1] * 768,
        profile=profile,
    )

    assert isinstance(record, EmbeddingProfileVector)
    assert record.profile_name == "local-http-clip-native-image-768"
    assert record.profile_kind == "native_image"
    assert record.input_modality == "image"
    assert record.profile_metadata == {
        "enabled_by_default": False,
        "fallback_profile_name": "openai-text-embedding-3-small-1536",
        "requires_api_key": False,
    }
    assert record.embedding_768 == [0.1] * 768
    assert record.embedding_1536 is None


def test_multilingual_profile_uses_side_by_side_profile_metadata() -> None:
    profile = resolve_embedding_profile(
        profile_name="local-http-bge-m3-multilingual-1024",
        experimental_profiles_enabled=True,
    )

    record = embedding_record_for_profile(
        item_id=uuid.uuid4(),
        chunk_index=0,
        chunk_text="contrato en espanol",
        vector=[0.2] * 1024,
        profile=profile,
    )

    assert isinstance(record, EmbeddingProfileVector)
    assert record.profile_kind == "multilingual_text"
    assert record.input_modality == "multilingual_text"
    assert record.profile_metadata["fallback_profile_name"] == "openai-text-embedding-3-small-1536"
