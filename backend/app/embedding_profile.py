from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


DEFAULT_EMBEDDING_PROVIDER = "openai"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_PROFILE_NAME = "openai-text-embedding-3-small-1536"
EMBEDDING_DIMENSIONS = 1536
DEFAULT_LOCAL_HTTP_EMBEDDING_PATH = "/embed"
SUPPORTED_PROFILE_VECTOR_DIMENSIONS = frozenset({384, 768, 1024, EMBEDDING_DIMENSIONS})

SUPPORTED_EMBEDDING_PROVIDERS = frozenset({"openai", "local-http"})
SUPPORTED_PROFILE_KINDS = frozenset({"text", "native_image", "multilingual_text"})
SUPPORTED_INPUT_MODALITIES = frozenset({"text", "image", "multilingual_text"})

_DIMENSIONAL_OPENAI_MODELS = {
    "text-embedding-3-small",
    "text-embedding-3-large",
}


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: str
    model: str
    dimensions: int
    profile_name: str
    profile_kind: str = "text"
    input_modality: str = "text"
    enabled_by_default: bool = True
    fallback_profile_name: str | None = DEFAULT_EMBEDDING_PROFILE_NAME
    max_batch_size: int = 2048
    max_input_tokens: int | None = None
    query_instruction: str | None = None
    document_instruction: str | None = None
    recommended_cpu: bool = False
    requires_api_key: bool = False


@dataclass(frozen=True)
class EmbeddingModelProfile:
    provider: Literal["openai", "local-http"]
    model: str
    dimensions: int
    profile_name: str
    max_batch_size: int
    profile_kind: Literal["text", "native_image", "multilingual_text"] = "text"
    input_modality: Literal["text", "image", "multilingual_text"] = "text"
    enabled_by_default: bool = True
    fallback_profile_name: str | None = DEFAULT_EMBEDDING_PROFILE_NAME
    max_input_tokens: int | None = None
    query_instruction: str | None = None
    document_instruction: str | None = None
    recommended_cpu: bool = False
    requires_api_key: bool = False


EMBEDDING_MODEL_PROFILES = {
    DEFAULT_EMBEDDING_PROFILE_NAME: EmbeddingModelProfile(
        provider="openai",
        model=DEFAULT_EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        profile_name=DEFAULT_EMBEDDING_PROFILE_NAME,
        max_batch_size=2048,
    ),
    "local-http-gte-modernbert-base": EmbeddingModelProfile(
        provider="local-http",
        model="Alibaba-NLP/gte-modernbert-base",
        dimensions=768,
        profile_name="local-http-gte-modernbert-base",
        max_batch_size=32,
        max_input_tokens=8192,
        recommended_cpu=True,
    ),
    "local-http-bge-small-en-v1.5": EmbeddingModelProfile(
        provider="local-http",
        model="BAAI/bge-small-en-v1.5",
        dimensions=384,
        profile_name="local-http-bge-small-en-v1.5",
        max_batch_size=64,
        max_input_tokens=512,
        query_instruction="query: ",
        document_instruction="passage: ",
        recommended_cpu=True,
    ),
    "local-http-qwen3-embedding-0.6b": EmbeddingModelProfile(
        provider="local-http",
        model="Qwen/Qwen3-Embedding-0.6B",
        dimensions=1024,
        profile_name="local-http-qwen3-embedding-0.6b",
        max_batch_size=16,
        max_input_tokens=32768,
        recommended_cpu=False,
    ),
    "local-http-clip-native-image-768": EmbeddingModelProfile(
        provider="local-http",
        model="openai/clip-vit-large-patch14",
        dimensions=768,
        profile_name="local-http-clip-native-image-768",
        max_batch_size=16,
        profile_kind="native_image",
        input_modality="image",
        enabled_by_default=False,
        max_input_tokens=None,
        recommended_cpu=False,
    ),
    "local-http-bge-m3-multilingual-1024": EmbeddingModelProfile(
        provider="local-http",
        model="BAAI/bge-m3",
        dimensions=1024,
        profile_name="local-http-bge-m3-multilingual-1024",
        max_batch_size=32,
        profile_kind="multilingual_text",
        input_modality="multilingual_text",
        enabled_by_default=False,
        max_input_tokens=8192,
        recommended_cpu=True,
    ),
}


def resolve_embedding_profile(
    *,
    provider: str | None = None,
    model: str | None = None,
    dimensions: int | None = None,
    profile_name: str | None = None,
    experimental_profiles_enabled: bool = False,
) -> EmbeddingProfile:
    normalized_profile_name = (profile_name or "").strip()
    model_profile = EMBEDDING_MODEL_PROFILES.get(normalized_profile_name)

    if model_profile:
        supplied_provider = (provider or "").strip().lower()
        if (
            normalized_profile_name != DEFAULT_EMBEDDING_PROFILE_NAME
            and supplied_provider
            and supplied_provider != model_profile.provider
        ):
            raise ValueError(
                f"EMBEDDING_PROVIDER {supplied_provider!r} does not match "
                f"embedding profile {normalized_profile_name!r} provider {model_profile.provider!r}"
            )
        supplied_model = (model or "").strip()
        model_alias = model_profile.model.rsplit("/", 1)[-1]
        if (
            normalized_profile_name != DEFAULT_EMBEDDING_PROFILE_NAME
            and supplied_model
            and supplied_model != DEFAULT_EMBEDDING_MODEL
            and supplied_model != model_profile.model
            and supplied_model != model_alias
        ):
            raise ValueError(
                f"EMBEDDING_MODEL {supplied_model!r} does not match "
                f"embedding profile {normalized_profile_name!r} model {model_profile.model!r}"
            )
        if (
            normalized_profile_name != DEFAULT_EMBEDDING_PROFILE_NAME
            and dimensions not in {None, EMBEDDING_DIMENSIONS, model_profile.dimensions}
        ):
            raise ValueError(
                f"EMBEDDING_DIMENSIONS {dimensions!r} does not match "
                f"embedding profile {normalized_profile_name!r} dimensions {model_profile.dimensions}"
            )
        resolved_provider = (
            model_profile.provider
            if normalized_profile_name != DEFAULT_EMBEDDING_PROFILE_NAME
            else provider or model_profile.provider
        )
        resolved_model = None if model in {None, "", DEFAULT_EMBEDDING_MODEL} else model
        resolved_model = resolved_model or model_profile.model
    else:
        resolved_provider = provider or DEFAULT_EMBEDDING_PROVIDER
        resolved_model = model or DEFAULT_EMBEDDING_MODEL

    normalized_provider = resolved_provider.strip().lower()
    normalized_model = resolved_model.strip()
    if model_profile and dimensions in {None, EMBEDDING_DIMENSIONS}:
        normalized_dimensions = model_profile.dimensions
    else:
        normalized_dimensions = EMBEDDING_DIMENSIONS if dimensions is None else dimensions

    if normalized_provider not in SUPPORTED_EMBEDDING_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_EMBEDDING_PROVIDERS))
        raise ValueError(f"EMBEDDING_PROVIDER must be one of {supported}; got {provider!r}")
    if not normalized_model:
        raise ValueError("EMBEDDING_MODEL must not be empty")
    if normalized_dimensions <= 0:
        raise ValueError(f"EMBEDDING_DIMENSIONS must be greater than 0; got {normalized_dimensions}")
    if normalized_dimensions not in SUPPORTED_PROFILE_VECTOR_DIMENSIONS:
        supported_dimensions = ", ".join(str(value) for value in sorted(SUPPORTED_PROFILE_VECTOR_DIMENSIONS))
        raise ValueError(
            f"EMBEDDING_DIMENSIONS must be one of {supported_dimensions}; "
            f"got {normalized_dimensions}"
        )

    if not normalized_profile_name:
        normalized_profile_name = f"{normalized_provider}-{normalized_model}-{normalized_dimensions}"

    profile_kind = model_profile.profile_kind if model_profile else "text"
    input_modality = model_profile.input_modality if model_profile else "text"
    if profile_kind not in SUPPORTED_PROFILE_KINDS:
        supported_kinds = ", ".join(sorted(SUPPORTED_PROFILE_KINDS))
        raise ValueError(f"embedding profile kind must be one of {supported_kinds}; got {profile_kind!r}")
    if input_modality not in SUPPORTED_INPUT_MODALITIES:
        supported_modalities = ", ".join(sorted(SUPPORTED_INPUT_MODALITIES))
        raise ValueError(
            f"embedding profile input modality must be one of {supported_modalities}; got {input_modality!r}"
        )
    enabled_by_default = model_profile.enabled_by_default if model_profile else True
    if not enabled_by_default and not experimental_profiles_enabled:
        raise ValueError(
            f"embedding profile {normalized_profile_name!r} is disabled by default; "
            "set EMBEDDING_EXPERIMENTAL_PROFILES_ENABLED=true before using it"
        )

    return EmbeddingProfile(
        provider=normalized_provider,
        model=normalized_model,
        dimensions=normalized_dimensions,
        profile_name=normalized_profile_name,
        profile_kind=profile_kind,
        input_modality=input_modality,
        enabled_by_default=enabled_by_default,
        fallback_profile_name=model_profile.fallback_profile_name if model_profile else DEFAULT_EMBEDDING_PROFILE_NAME,
        max_batch_size=model_profile.max_batch_size if model_profile else 2048,
        max_input_tokens=model_profile.max_input_tokens if model_profile else None,
        query_instruction=model_profile.query_instruction if model_profile else None,
        document_instruction=model_profile.document_instruction if model_profile else None,
        recommended_cpu=model_profile.recommended_cpu if model_profile else False,
        requires_api_key=model_profile.requires_api_key if model_profile else False,
    )


def embedding_request_dimensions(model: str) -> int | None:
    """Return the explicit dimensions parameter for models that support shortening."""
    if model in _DIMENSIONAL_OPENAI_MODELS:
        return EMBEDDING_DIMENSIONS
    return None


def is_default_embedding_profile(profile: EmbeddingProfile) -> bool:
    return (
        profile.provider == DEFAULT_EMBEDDING_PROVIDER
        and profile.model == DEFAULT_EMBEDDING_MODEL
        and profile.dimensions == EMBEDDING_DIMENSIONS
        and profile.profile_name == DEFAULT_EMBEDDING_PROFILE_NAME
    )
