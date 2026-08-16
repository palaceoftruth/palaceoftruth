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


def _redis_settings_stub(**overrides):
    values = {
        "redis_sentinel_hosts": "",
        "redis_sentinel_master": "mymaster",
        "redis_url": "redis://unused:6379",
        "redis_username": "",
        "redis_password": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_make_redis_settings_uses_sentinel_host_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config,
        "settings",
        _redis_settings_stub(
            redis_sentinel_hosts="valkey-sentinel:26379, backup-sentinel:26380",
            redis_sentinel_master="palace-primary",
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
        _redis_settings_stub(redis_sentinel_hosts=", ,"),
    )

    with pytest.raises(ValueError, match="REDIS_SENTINEL_HOSTS"):
        config.make_redis_settings()


def test_make_redis_settings_applies_credentials_to_the_sentinel_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "settings",
        _redis_settings_stub(
            redis_sentinel_hosts="valkey-sentinel:26379",
            redis_password="s3cret",
        ),
    )

    redis_settings = config.make_redis_settings()

    assert redis_settings.password == "s3cret"


def test_redis_credentials_override_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The URL lives in a ConfigMap; the password comes from a Secret and wins.
    monkeypatch.setattr(
        config,
        "settings",
        _redis_settings_stub(
            redis_url="redis://stale:old-password@valkey:6379",
            redis_username="palace",
            redis_password="s3cret",
        ),
    )

    redis_settings = config.make_redis_settings()

    assert redis_settings.username == "palace"
    assert redis_settings.password == "s3cret"


def test_redis_settings_stay_anonymous_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "settings", _redis_settings_stub())

    redis_settings = config.make_redis_settings()

    assert redis_settings.password is None


def test_settings_keep_openai_embedding_profile_defaults() -> None:
    settings = config.Settings(**_settings_kwargs())

    assert settings.embedding_provider == "openai"
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_dimensions == 1536
    assert settings.embedding_profile_name == "openai-text-embedding-3-small-1536"


def test_browser_sessions_default_to_thirty_days_for_all_scopes() -> None:
    settings = config.Settings(**_settings_kwargs())

    assert settings.browser_session_ttl_seconds == 2_592_000
    assert settings.elevated_browser_session_ttl_seconds == 2_592_000


def test_settings_validation_errors_do_not_disclose_secret_inputs() -> None:
    secret_values = {
        "database_url": "postgresql+asyncpg://palace:database-secret@example.test/palace",
        "openai_api_key": "openai-secret-sentinel",
        "openrouter_api_key": "openrouter-secret-sentinel",
        "api_key": "api-secret-sentinel",
        "github_pat": "github-secret-sentinel",
    }

    with pytest.raises(ValidationError) as exc_info:
        config.Settings(
            **secret_values,
            relationship_classification_temperature=3,
        )

    error_message = str(exc_info.value)
    assert "RELATIONSHIP_CLASSIFICATION_TEMPERATURE" in error_message
    for secret in secret_values.values():
        assert secret not in error_message


def test_settings_expose_frozen_relationship_classifier_profile() -> None:
    settings = config.Settings(**_settings_kwargs())

    assert settings.relationship_classification_model == "openai/gpt-4.1"
    assert settings.relationship_classification_temperature == 0.0
    assert settings.relationship_classification_seed == 1083
    assert settings.relationship_extraction_min_confidence == 0.7


def test_settings_expose_dedicated_openrouter_vision_chain_defaults() -> None:
    settings = config.Settings(**_settings_kwargs())

    assert settings.openrouter_vision_model == "minimax/minimax-m3"
    assert settings.openrouter_vision_fallback_models == (
        "openai/gpt-4o-mini,openai/gpt-4.1-mini"
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("relationship_classification_temperature", 2.1, "RELATIONSHIP_CLASSIFICATION_TEMPERATURE"),
        ("relationship_classification_seed", -1, "RELATIONSHIP_CLASSIFICATION_SEED"),
        ("relationship_extraction_min_confidence", 1.1, "RELATIONSHIP_EXTRACTION_MIN_CONFIDENCE"),
    ],
)
def test_settings_reject_invalid_relationship_classifier_profile(
    field: str,
    value,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        config.Settings(**_settings_kwargs(**{field: value}))


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


def test_settings_transcription_fallback_to_openai_defaults_on_and_is_overridable() -> None:
    assert config.Settings(**_settings_kwargs()).transcription_fallback_to_openai is True

    disabled = config.Settings(
        **_settings_kwargs(
            transcription_provider="local_whisperx",
            transcription_fallback_to_openai=False,
        )
    )
    assert disabled.transcription_fallback_to_openai is False


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


# B-03 / M-05: refuse to boot on any credential that is still the exact
# placeholder published in .env.example.
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("api_key", "change_me_api_key", "API_KEY"),
        ("openai_api_key", "sk-...", "OPENAI_API_KEY"),
        ("openrouter_api_key", "sk-or-...", "OPENROUTER_API_KEY"),
        ("credential_pepper", "change_me_api_key", "CREDENTIAL_PEPPER"),
        ("redis_password", "change_me_redis_password", "REDIS_PASSWORD"),
    ],
)
def test_settings_reject_placeholder_credential_values(field: str, value: str, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        config.Settings(**_settings_kwargs(**{field: value}))


def test_settings_accept_empty_redis_password_for_unauthenticated_local_redis() -> None:
    # redis_password defaults to "" for a local Redis with no auth configured;
    # that is not itself a placeholder and must not be rejected.
    settings = config.Settings(**_settings_kwargs(redis_password=""))

    assert settings.redis_password == ""


def test_settings_reject_placeholder_password_embedded_in_database_url() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        config.Settings(
            **_settings_kwargs(
                database_url="postgresql+asyncpg://palace:change_me_secure_password@example.test/palace"
            )
        )


def test_settings_reject_placeholder_admin_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALACEOFTRUTH_ADMIN_SECRET", "change_me_admin_secret")

    with pytest.raises(ValidationError, match="PALACEOFTRUTH_ADMIN_SECRET"):
        config.Settings(**_settings_kwargs())


def test_settings_accept_real_admin_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALACEOFTRUTH_ADMIN_SECRET", "a-real-admin-secret")

    config.Settings(**_settings_kwargs())


def test_settings_require_explicit_opt_in_for_raw_retrieval_queries() -> None:
    with pytest.raises(ValidationError, match="RETRIEVAL_CAPTURE_ALLOW_RAW_QUERIES"):
        config.Settings(**_settings_kwargs(retrieval_capture_query_mode="raw"))

    settings = config.Settings(
        **_settings_kwargs(
            retrieval_capture_query_mode="raw",
            retrieval_capture_allow_raw_queries=True,
        )
    )
    assert settings.retrieval_capture_query_mode == "raw"


def test_settings_reject_invalid_tenant_llm_limits() -> None:
    with pytest.raises(ValidationError, match="TENANT_LLM_MAX_CONCURRENT_REQUESTS"):
        config.Settings(**_settings_kwargs(tenant_llm_max_concurrent_requests=0))
    with pytest.raises(ValidationError, match="TENANT_LLM_DAILY_TOKEN_LIMIT"):
        config.Settings(**_settings_kwargs(tenant_llm_daily_token_limit=-1))


# A-05: chart-driven deployments always populate DEPLOYMENT_CLUSTER; local
# dev and the test suite never do. The pepper is mandatory only there.
def test_settings_require_credential_pepper_when_deployment_cluster_is_set() -> None:
    with pytest.raises(ValidationError, match="CREDENTIAL_PEPPER"):
        config.Settings(**_settings_kwargs(deployment_cluster="rke2-abby", credential_pepper=""))


def test_settings_accept_missing_credential_pepper_without_deployment_cluster() -> None:
    settings = config.Settings(**_settings_kwargs(credential_pepper=""))

    assert settings.credential_pepper == ""


def test_settings_accept_credential_pepper_with_deployment_cluster_set() -> None:
    settings = config.Settings(
        **_settings_kwargs(
            deployment_cluster="rke2-abby",
            credential_pepper="a-real-pepper-value",
            database_url="postgresql+asyncpg://palace:secret@example.test/palace?sslmode=verify-full",
            database_ssl_root_cert="/etc/palaceoftruth/database-tls/ca.crt",
        )
    )

    assert settings.credential_pepper == "a-real-pepper-value"


def test_settings_require_verified_database_tls_in_deployment_cluster() -> None:
    with pytest.raises(ValidationError, match="sslmode=verify-full"):
        config.Settings(
            **_settings_kwargs(
                deployment_cluster="rke2-abby",
                credential_pepper="a-real-pepper-value",
            )
        )
