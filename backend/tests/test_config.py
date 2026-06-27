from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import config


def _settings_kwargs(**overrides):
    values = {
        "database_url": "postgresql+asyncpg://palace:secret@example.test/palace",
        "openai_api_key": "test-openai-key",
        "openrouter_api_key": "test-openrouter-key",
        "api_key": "test-api-key",
    }
    values.update(overrides)
    return values


def test_make_redis_settings_uses_sentinel_host_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            redis_sentinel_hosts="valkey-sentinel:26379, backup-sentinel:26380",
            redis_sentinel_master="palace-primary",
            redis_url="redis://unused:6379",
        ),
    )

    redis_settings = config.make_redis_settings()

    assert redis_settings.sentinel is True
    assert redis_settings.sentinel_master == "palace-primary"
    assert redis_settings.host == [
        ("valkey-sentinel", 26379),
        ("backup-sentinel", 26380),
    ]


def test_make_redis_settings_rejects_empty_sentinel_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            redis_sentinel_hosts=", ,",
            redis_sentinel_master="mymaster",
            redis_url="redis://unused:6379",
        ),
    )

    with pytest.raises(ValueError, match="REDIS_SENTINEL_HOSTS"):
        config.make_redis_settings()


def test_settings_keep_openai_embedding_profile_defaults() -> None:
    settings = config.Settings(**_settings_kwargs())

    assert settings.embedding_provider == "openai"
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_dimensions == 1536
    assert settings.embedding_profile_name == "openai-text-embedding-3-small-1536"


def test_settings_reject_unknown_embedding_provider() -> None:
    with pytest.raises(ValidationError, match="EMBEDDING_PROVIDER"):
        config.Settings(**_settings_kwargs(embedding_provider="unknown"))


def test_settings_reject_unsupported_embedding_dimension() -> None:
    with pytest.raises(ValidationError, match="EMBEDDING_DIMENSIONS"):
        config.Settings(**_settings_kwargs(embedding_dimensions=2048))


def test_settings_accept_local_http_side_by_side_profile_dimensions() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            embedding_provider="local-http",
            embedding_model="gte-modernbert-base",
            embedding_dimensions=768,
            embedding_profile_name="local-http-gte-modernbert-base",
            embedding_local_http_url="http://embedding.test",
        )
    )

    assert settings.embedding_provider == "local-http"
    assert settings.embedding_dimensions == 768
    assert settings.embedding_profile_name == "local-http-gte-modernbert-base"


def test_settings_reject_disabled_native_profile_without_opt_in() -> None:
    with pytest.raises(ValidationError, match="disabled by default"):
        config.Settings(
            **_settings_kwargs(
                embedding_provider="local-http",
                embedding_profile_name="local-http-clip-native-image-768",
                embedding_local_http_url="http://embedding.test",
            )
        )


def test_settings_accept_disabled_native_profile_with_explicit_opt_in() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            embedding_provider="local-http",
            embedding_profile_name="local-http-clip-native-image-768",
            embedding_experimental_profiles_enabled=True,
            embedding_local_http_url="http://embedding.test",
        )
    )

    assert settings.embedding_provider == "local-http"
    assert settings.embedding_model == "openai/clip-vit-large-patch14"
    assert settings.embedding_dimensions == 768
    assert settings.embedding_profile_name == "local-http-clip-native-image-768"


def test_settings_reject_native_profile_provider_mismatch() -> None:
    with pytest.raises(ValidationError, match="does not match embedding profile"):
        config.Settings(
            **_settings_kwargs(
                embedding_provider="openai",
                embedding_profile_name="local-http-clip-native-image-768",
                embedding_experimental_profiles_enabled=True,
                embedding_local_http_url="http://embedding.test",
            )
        )


def test_settings_reject_disabled_multilingual_profile_without_opt_in() -> None:
    with pytest.raises(ValidationError, match="disabled by default"):
        config.Settings(
            **_settings_kwargs(
                embedding_provider="local-http",
                embedding_profile_name="local-http-bge-m3-multilingual-1024",
                embedding_local_http_url="http://embedding.test",
            )
        )


def test_settings_accept_disabled_multilingual_profile_with_explicit_opt_in() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            embedding_provider="local-http",
            embedding_profile_name="local-http-bge-m3-multilingual-1024",
            embedding_experimental_profiles_enabled=True,
            embedding_local_http_url="http://embedding.test",
        )
    )

    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.embedding_dimensions == 1024
    assert settings.embedding_profile_name == "local-http-bge-m3-multilingual-1024"


def test_settings_infer_catalog_profile_dimensions_when_named_profile_is_selected() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            embedding_provider="local-http",
            embedding_profile_name="local-http-bge-small-en-v1.5",
            embedding_local_http_url="http://embedding.test",
        )
    )

    assert settings.embedding_model == "BAAI/bge-small-en-v1.5"
    assert settings.embedding_dimensions == 384


def test_settings_accept_local_http_profile_contract() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            embedding_provider="local-http",
            embedding_model="gte-modernbert-base",
            embedding_dimensions=1536,
            embedding_profile_name="local-http-gte-modernbert-base-1536",
            embedding_local_http_url="http://embedding.test",
        )
    )

    assert settings.embedding_provider == "local-http"
    assert settings.embedding_model == "gte-modernbert-base"
    assert settings.embedding_dimensions == 1536


def test_settings_reject_local_http_without_endpoint_url() -> None:
    with pytest.raises(ValidationError, match="EMBEDDING_LOCAL_HTTP_URL"):
        config.Settings(
            **_settings_kwargs(
                embedding_provider="local-http",
                embedding_model="gte-modernbert-base",
                embedding_dimensions=1536,
            )
        )


def test_settings_reject_local_http_invalid_path() -> None:
    with pytest.raises(ValidationError, match="EMBEDDING_LOCAL_HTTP_PATH"):
        config.Settings(
            **_settings_kwargs(
                embedding_provider="local-http",
                embedding_model="gte-modernbert-base",
                embedding_dimensions=1536,
                embedding_local_http_url="http://embedding.test",
                embedding_local_http_path="embed",
            )
        )


def test_settings_reject_local_http_non_positive_timeout() -> None:
    with pytest.raises(ValidationError, match="EMBEDDING_LOCAL_HTTP_TIMEOUT_SECONDS"):
        config.Settings(
            **_settings_kwargs(
                embedding_provider="local-http",
                embedding_model="gte-modernbert-base",
                embedding_dimensions=1536,
                embedding_local_http_url="http://embedding.test",
                embedding_local_http_timeout_seconds=0,
            )
        )


def test_settings_accept_assemblyai_transcription_provider() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            transcription_provider="AssemblyAI",
            assemblyai_base_url="https://api.assemblyai.com",
            assemblyai_poll_interval_seconds=1.5,
        )
    )

    assert settings.transcription_provider == "assemblyai"
    assert settings.transcription_max_parallel_chunks == 2


def test_settings_accept_local_whisperx_transcription_provider() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            transcription_provider="local_whisperx",
            llm_gateway_url="http://llm-gateway.test:8080",
            llm_gateway_token="gateway-token",
            local_whisperx_model="whisperx/small",
        )
    )

    assert settings.transcription_provider == "local_whisperx"
    assert settings.llm_gateway_url == "http://llm-gateway.test:8080"
    assert settings.llm_gateway_token == "gateway-token"
    assert settings.local_whisperx_model == "whisperx/small"


def test_settings_reject_unknown_transcription_provider() -> None:
    with pytest.raises(ValidationError, match="TRANSCRIPTION_PROVIDER"):
        config.Settings(**_settings_kwargs(transcription_provider="local-whisper"))


def test_settings_reject_invalid_llm_gateway_url() -> None:
    with pytest.raises(ValidationError, match="LLM_GATEWAY_URL"):
        config.Settings(**_settings_kwargs(llm_gateway_url="not-a-url"))


def test_settings_reject_invalid_assemblyai_base_url() -> None:
    with pytest.raises(ValidationError, match="ASSEMBLYAI_BASE_URL"):
        config.Settings(**_settings_kwargs(assemblyai_base_url="not-a-url"))


def test_settings_reject_non_positive_parallel_transcription_chunks() -> None:
    with pytest.raises(ValidationError, match="TRANSCRIPTION_MAX_PARALLEL_CHUNKS"):
        config.Settings(**_settings_kwargs(transcription_max_parallel_chunks=0))


def test_settings_accept_firecrawl_self_hosted_without_api_key() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            webpage_scraper_provider="firecrawl-self-hosted",
            firecrawl_base_url="https://firecrawl.internal.example/v2",
            firecrawl_api_key="",
        )
    )

    assert settings.webpage_scraper_provider == "firecrawl-self-hosted"
    assert settings.firecrawl_base_url == "https://firecrawl.internal.example/v2"


def test_settings_require_firecrawl_cloud_api_key() -> None:
    with pytest.raises(ValidationError, match="FIRECRAWL_API_KEY"):
        config.Settings(**_settings_kwargs(webpage_scraper_provider="firecrawl-cloud", firecrawl_api_key=""))


def test_settings_reject_invalid_firecrawl_base_url_when_enabled() -> None:
    with pytest.raises(ValidationError, match="FIRECRAWL_BASE_URL"):
        config.Settings(
            **_settings_kwargs(
                webpage_scraper_provider="firecrawl-self-hosted",
                firecrawl_base_url="not-a-url",
            )
        )
