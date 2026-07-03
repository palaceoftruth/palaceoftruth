from __future__ import annotations

from pydantic import model_validator
from urllib.parse import urlparse
from pydantic_settings import BaseSettings
from app.embedding_profile import (
    DEFAULT_LOCAL_HTTP_EMBEDDING_PATH,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROFILE_NAME,
    DEFAULT_EMBEDDING_PROVIDER,
    EMBEDDING_DIMENSIONS,
    resolve_embedding_profile,
)


class Settings(BaseSettings):
    # Database
    database_url: str

    # Redis — standard connection
    redis_url: str = "redis://localhost:6379"
    # Redis Sentinel — when set, overrides redis_url for ARQ connections.
    # Format: comma-separated "host:port" entries, e.g. "valkey-sentinel:26379".
    redis_sentinel_hosts: str = ""
    redis_sentinel_master: str = "mymaster"

    # OpenAI (embeddings + transcription + vision)
    openai_api_key: str
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimensions: int = EMBEDDING_DIMENSIONS
    embedding_profile_name: str = DEFAULT_EMBEDDING_PROFILE_NAME
    embedding_experimental_profiles_enabled: bool = False
    embedding_local_http_url: str = ""
    embedding_local_http_path: str = DEFAULT_LOCAL_HTTP_EMBEDDING_PATH
    embedding_local_http_api_key: str = ""
    embedding_local_http_timeout_seconds: float = 30.0
    embedding_local_http_normalize: bool = True
    embedding_local_http_truncate: bool = True
    whisper_model: str = "gpt-4o-transcribe-diarize"
    transcription_provider: str = "openai"
    llm_gateway_url: str = "http://llm-gateway.example:8080"
    llm_gateway_token: str = ""
    local_whisperx_model: str = "whisperx/base"
    assemblyai_api_key: str = ""
    assemblyai_base_url: str = "https://api.assemblyai.com"
    assemblyai_speech_model: str = "universal-2"
    assemblyai_poll_interval_seconds: float = 2.0
    transcription_max_duration_seconds: int = 1400
    transcription_max_chunk_seconds: int = 600
    transcription_max_upload_bytes: int = 25 * 1024 * 1024
    media_download_timeout_seconds: int = 1500
    media_ffmpeg_timeout_seconds: int = 300
    media_tenant_fair_per_tenant_inflight_limit: int = 1
    media_tenant_fair_dispatch_batch_size: int = 2
    media_tenant_fair_candidate_limit: int = 100
    transcription_request_timeout_seconds: int = 300
    transcription_transient_retries: int = 2
    transcription_retry_backoff_seconds: float = 2.0
    transcription_max_parallel_chunks: int = 2
    vision_model: str = "gpt-4o-mini"

    # OpenRouter (LLM)
    openrouter_api_key: str
    openrouter_default_model: str = "minimax/minimax-m2.7"
    openrouter_fallback_models: str = "nvidia/nemotron-3-super-120b-a12b"

    # API auth
    api_key: str
    cors_allowed_origins: str = "https://palace.sarvent.cloud,https://palaceoftruth.test,http://localhost:3000"

    # Chunking
    chunk_size: int = 500
    chunk_overlap: int = 50
    search_limit: int = 10
    upload_artifact_dir: str = "/tmp/palaceoftruth/upload-artifacts"
    app_version: str = ""

    # Retrieval capture is an opt-in local artifact for ranking replay. The
    # default stores only query fingerprints, never raw query text.
    retrieval_capture_enabled: bool = False
    retrieval_capture_path: str = "/tmp/palaceoftruth/retrieval-capture.ndjson"
    retrieval_capture_query_mode: str = "fingerprint"
    retrieval_capture_max_query_chars: int = 500
    retrieval_source_ranking_enabled: bool = True
    retrieval_relationship_expansion_enabled: bool = False
    retrieval_relationship_min_confidence: float = 0.7
    retrieval_relationship_fanout_limit: int = 3
    retrieval_relationship_hop_decay: float = 0.72
    retrieval_relationship_max_bonus: float = 0.05
    retrieval_hint_ranking_enabled: bool = False
    retrieval_hint_ranking_max_bonus: float = 0.06
    retrieval_hint_report_enabled: bool = False
    retrieval_hint_report_limit: int = 5
    retrieval_hint_rescue_enabled: bool = False
    retrieval_hint_rescue_min_score: float = 0.8
    retrieval_hint_rescue_limit: int = 3
    retrieval_second_stage_reranker_enabled: bool = False
    retrieval_second_stage_reranker_provider: str = ""
    retrieval_second_stage_reranker_candidate_limit: int = 20
    retrieval_second_stage_reranker_timeout_ms: int = 150
    retrieval_second_stage_reranker_max_bonus: float = 0.08
    palaceoftruth_delegated_agent_memory_read_policies: str = ""

    # Feed polling
    feed_poll_min_interval: int = 300   # floor for poll_interval (5 minutes)
    feed_max_failures: int = 5          # auto-disable threshold
    source_subscription_poll_min_interval: int = 900
    source_subscription_max_failures: int = 5
    source_subscription_manual_sync_cooldown_seconds: int = 300
    source_subscription_stale_queued_minutes: int = 60

    # Social post capture
    facebook_oembed_access_token: str = ""
    facebook_graph_api_version: str = "v25.0"

    # Webpage scraping provider. Firecrawl is opt-in so existing local
    # trafilatura/Playwright scraping remains the default.
    webpage_scraper_provider: str = "local"
    firecrawl_base_url: str = "https://api.firecrawl.dev/v2"
    firecrawl_api_key: str = ""
    firecrawl_timeout_seconds: float = 60.0
    firecrawl_only_main_content: bool = True

    # Palace sync
    palace_sync_allowed_roots: str = ""
    palace_sync_max_file_bytes: int = 250_000
    palace_repo_checkout_root: str = "/tmp/palaceoftruth/repo-checkouts"
    palace_default_s3_source_name: str = ""
    palace_default_s3_bucket: str = ""
    palace_default_s3_prefix: str = ""
    palace_default_s3_endpoint_url: str = ""
    palace_default_s3_region: str = ""
    palace_default_s3_allowed_extensions: str = ".md"
    palace_default_s3_scan_interval_seconds: int = 900
    palace_default_s3_force_path_style: bool = False
    palace_sync_watcher_enabled: bool = False
    palace_sync_watcher_probe_seconds: int = 10
    github_pat: str = ""
    palaceoftruth_sync_source_credential_key: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    @model_validator(mode="after")
    def validate_embedding_profile(self) -> "Settings":
        profile = resolve_embedding_profile(
            provider=self.embedding_provider,
            model=self.embedding_model,
            dimensions=self.embedding_dimensions,
            profile_name=self.embedding_profile_name,
            experimental_profiles_enabled=self.embedding_experimental_profiles_enabled,
        )
        self.embedding_provider = profile.provider
        self.embedding_model = profile.model
        self.embedding_dimensions = profile.dimensions
        self.embedding_profile_name = profile.profile_name
        if profile.provider == "local-http":
            parsed = urlparse(self.embedding_local_http_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("EMBEDDING_LOCAL_HTTP_URL must be an http(s) URL for local-http embeddings")
            if not self.embedding_local_http_path.startswith("/"):
                raise ValueError("EMBEDDING_LOCAL_HTTP_PATH must start with /")
            if self.embedding_local_http_timeout_seconds <= 0:
                raise ValueError("EMBEDDING_LOCAL_HTTP_TIMEOUT_SECONDS must be greater than 0")
            if profile.requires_api_key and not self.embedding_local_http_api_key.strip():
                raise ValueError(
                    f"EMBEDDING_LOCAL_HTTP_API_KEY is required for embedding profile {profile.profile_name!r}"
                )
        transcription_provider = self.transcription_provider.strip().lower()
        if transcription_provider not in {"openai", "assemblyai", "local_whisperx"}:
            raise ValueError("TRANSCRIPTION_PROVIDER must be one of: openai, assemblyai, local_whisperx")
        self.transcription_provider = transcription_provider
        if self.llm_gateway_url:
            parsed = urlparse(self.llm_gateway_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("LLM_GATEWAY_URL must be an http(s) URL")
        if self.assemblyai_base_url:
            parsed = urlparse(self.assemblyai_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("ASSEMBLYAI_BASE_URL must be an http(s) URL")
        if self.assemblyai_poll_interval_seconds <= 0:
            raise ValueError("ASSEMBLYAI_POLL_INTERVAL_SECONDS must be greater than 0")
        if self.transcription_max_parallel_chunks < 1:
            raise ValueError("TRANSCRIPTION_MAX_PARALLEL_CHUNKS must be at least 1")
        webpage_scraper_provider = self.webpage_scraper_provider.strip().lower().replace("_", "-")
        if webpage_scraper_provider not in {"local", "firecrawl-cloud", "firecrawl-self-hosted"}:
            raise ValueError(
                "WEBPAGE_SCRAPER_PROVIDER must be one of: local, firecrawl-cloud, firecrawl-self-hosted"
            )
        self.webpage_scraper_provider = webpage_scraper_provider
        if webpage_scraper_provider.startswith("firecrawl"):
            parsed = urlparse(self.firecrawl_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("FIRECRAWL_BASE_URL must be an http(s) URL")
            if self.firecrawl_timeout_seconds <= 0:
                raise ValueError("FIRECRAWL_TIMEOUT_SECONDS must be greater than 0")
            if webpage_scraper_provider == "firecrawl-cloud" and not self.firecrawl_api_key.strip():
                raise ValueError("FIRECRAWL_API_KEY is required when WEBPAGE_SCRAPER_PROVIDER=firecrawl-cloud")
        return self


settings = Settings()


def make_redis_settings():  # type: ignore[return]
    """Return an ARQ-compatible RedisSettings for the configured Redis backend.

    When REDIS_SENTINEL_HOSTS is set, returns sentinel-aware settings that
    let ARQ (and the underlying redis-py client) automatically discover and
    reconnect to the current primary after a sentinel failover.
    """
    from arq.connections import RedisSettings

    if settings.redis_sentinel_hosts:
        sentinels: list[tuple[str, int]] = []
        for raw_host in settings.redis_sentinel_hosts.split(","):
            entry = raw_host.strip()
            if not entry:
                continue
            host, _, port_str = entry.partition(":")
            sentinels.append((host, int(port_str) if port_str else 26379))
        if not sentinels:
            raise ValueError("REDIS_SENTINEL_HOSTS must include at least one host")
        return RedisSettings(
            host=sentinels,
            sentinel=True,
            sentinel_master=settings.redis_sentinel_master,
        )
    return RedisSettings.from_dsn(settings.redis_url)
