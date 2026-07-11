import asyncio
import logging
from time import perf_counter
from typing import Any

import httpx
import tiktoken
from openai import APIConnectionError, APITimeoutError, APIStatusError, AsyncOpenAI, RateLimitError

from app.config import settings
from app.embedding_profile import (
    DEFAULT_EMBEDDING_MODEL,
    embedding_request_dimensions,
    resolve_embedding_profile,
)
from app.services.memory_telemetry import record_embedding_request

logger = logging.getLogger(__name__)

_MAX_BATCH = 2048
_MAX_INPUT_TOKENS = 8192
_MAX_BATCH_TOKENS = 285_000
_MAX_BATCH_BYTES = 16 * 1024 * 1024
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}
_QUOTA_ERROR_MARKERS = ("billing", "credit", "insufficient_quota", "quota")


class EmbeddingRequestError(RuntimeError):
    """A bounded provider/request failure with an explicit retry contract."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        failure_kind: str,
        provider_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.failure_kind = failure_kind
        self.provider_status_code = provider_status_code


class EmbeddingService:
    """Generates embeddings via the active embedding profile."""

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self.profile = resolve_embedding_profile(
            provider=settings.embedding_provider,
            model=settings.embedding_model or DEFAULT_EMBEDDING_MODEL,
            dimensions=settings.embedding_dimensions,
            profile_name=settings.embedding_profile_name,
            experimental_profiles_enabled=settings.embedding_experimental_profiles_enabled,
        )
        self.model = self.profile.model
        self.client = AsyncOpenAI(api_key=settings.openai_api_key) if self.profile.provider == "openai" else None
        self.http_client = http_client

        if self.profile.provider == "local-http":
            self.local_http_url = self._local_http_endpoint_url()
        else:
            self.local_http_url = ""

    async def __aenter__(self) -> "EmbeddingService":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()

    def _local_http_endpoint_url(self) -> str:
        base_url = settings.embedding_local_http_url.strip().rstrip("/")
        if not base_url:
            raise RuntimeError("EMBEDDING_LOCAL_HTTP_URL is required when EMBEDDING_PROVIDER=local-http")
        path = settings.embedding_local_http_path.strip()
        if not path.startswith("/"):
            raise RuntimeError("EMBEDDING_LOCAL_HTTP_PATH must start with /")
        return f"{base_url}{path}"

    def _local_http_client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.embedding_local_http_timeout_seconds)
            )
        return self.http_client

    async def embed_texts(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        """Generate ordered embeddings within provider count, token, and byte budgets."""
        if self.profile.input_modality == "image":
            raise RuntimeError(
                f"embedding profile {self.profile.profile_name!r} expects image inputs; "
                "use embed_image_references for report-only native image capture instead of text ingestion"
            )
        if not texts:
            return []

        results: list[list[float]] = []
        for batch in self._plan_text_batches(texts, input_type=input_type):
            embeddings = await self._embed_with_retry(batch, input_type=input_type)
            results.extend(embeddings)
        return results

    def _plan_text_batches(self, texts: list[str], *, input_type: str) -> list[list[str]]:
        request_texts = self._apply_input_instruction(texts, input_type=input_type)
        max_batch_size = min(_MAX_BATCH, self.profile.max_batch_size)
        max_input_tokens = self.profile.max_input_tokens
        if self.profile.provider == "openai":
            max_input_tokens = min(max_input_tokens or _MAX_INPUT_TOKENS, _MAX_INPUT_TOKENS)

        try:
            encoding = tiktoken.encoding_for_model(self.model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        current_bytes = 0
        for index, (original, request_text) in enumerate(zip(texts, request_texts, strict=True)):
            if not request_text.strip():
                error = EmbeddingRequestError(
                    f"Embedding input {index} must not be empty",
                    retryable=False,
                    failure_kind="validation",
                )
                self._record_terminal_error(error)
                raise error
            token_count = len(encoding.encode(request_text))
            byte_count = len(request_text.encode("utf-8"))
            if max_input_tokens is not None and token_count > max_input_tokens:
                error = EmbeddingRequestError(
                    f"Embedding input {index} has {token_count} tokens; maximum is {max_input_tokens}",
                    retryable=False,
                    failure_kind="input_too_large",
                )
                self._record_terminal_error(error)
                raise error
            if token_count > _MAX_BATCH_TOKENS or byte_count > _MAX_BATCH_BYTES:
                error = EmbeddingRequestError(
                    f"Embedding input {index} exceeds the request budget",
                    retryable=False,
                    failure_kind="input_too_large",
                )
                self._record_terminal_error(error)
                raise error

            would_overflow = current and (
                len(current) >= max_batch_size
                or current_tokens + token_count > _MAX_BATCH_TOKENS
                or current_bytes + byte_count > _MAX_BATCH_BYTES
            )
            if would_overflow:
                batches.append(current)
                current = []
                current_tokens = 0
                current_bytes = 0
            current.append(original)
            current_tokens += token_count
            current_bytes += byte_count

        if current:
            batches.append(current)
        return batches

    async def embed_single(self, text: str, *, input_type: str = "query") -> list[float]:
        """Generate embedding for a single text."""
        results = await self.embed_texts([text], input_type=input_type)
        return results[0]

    async def embed_image_references(self, image_references: list[str]) -> list[list[float]]:
        """Generate report-only native image embeddings from image references.

        The references are provider-specific strings, such as local file paths,
        signed URLs, or data URLs. This method intentionally does not store
        vectors or participate in the default text ingestion path.
        """
        if self.profile.input_modality != "image":
            raise RuntimeError(
                f"embedding profile {self.profile.profile_name!r} expects {self.profile.input_modality!r} inputs, "
                "not native image inputs"
            )
        if not image_references:
            return []
        return await self._embed_with_retry(image_references, input_type="image", validate_dimensions=False)

    async def _embed_with_retry(
        self,
        texts: list[str],
        *,
        input_type: str,
        validate_dimensions: bool = True,
    ) -> list[list[float]]:
        input_tokens = self._estimated_input_tokens(texts, input_type=input_type)
        for attempt in range(_MAX_RETRIES):
            attempt_started = perf_counter()
            try:
                if self.profile.provider == "local-http":
                    vectors = await self._embed_local_http(
                        texts,
                        input_type=input_type,
                        validate_dimensions=validate_dimensions,
                    )
                else:
                    request_dimensions = embedding_request_dimensions(self.model)
                    request_kwargs = {
                        "model": self.model,
                        "input": texts,
                    }
                    if request_dimensions is not None:
                        request_kwargs["dimensions"] = request_dimensions
                    if self.client is None:
                        raise RuntimeError("OpenAI embedding client is not initialized")
                    response = await self.client.embeddings.create(**request_kwargs)
                    # Require an exact index permutation before associating vectors
                    # with caller inputs; count alone cannot detect duplicates/gaps.
                    ordered_data = sorted(response.data, key=lambda item: item.index)
                    indices = [item.index for item in ordered_data]
                    if indices != list(range(len(texts))):
                        raise EmbeddingRequestError(
                            f"Embedding API returned invalid indices for {len(texts)} inputs",
                            retryable=False,
                            failure_kind="validation",
                        )
                    vectors = [item.embedding for item in ordered_data]
                    if validate_dimensions:
                        self._validate_dimensions(vectors)
                record_embedding_request(
                    status="success",
                    failure_kind="none",
                    retryable=False,
                    provider=self.profile.provider,
                    input_type=input_type,
                    duration_seconds=perf_counter() - attempt_started,
                    batch_size=len(texts),
                    input_tokens=input_tokens,
                )
                return vectors
            except EmbeddingRequestError as e:
                self._record_terminal_error(
                    e,
                    input_type=input_type,
                    started_at=attempt_started,
                    batch_size=len(texts),
                    input_tokens=input_tokens,
                )
                raise
            except httpx.HTTPStatusError as e:
                retryable = e.response.status_code in _RETRYABLE_HTTP_STATUS_CODES
                error = EmbeddingRequestError(
                    f"Embedding HTTP API returned {e.response.status_code}: {e.response.text[:200]}",
                    retryable=retryable,
                    failure_kind="http_status",
                    provider_status_code=e.response.status_code,
                )
                if not retryable:
                    self._record_terminal_error(
                        error,
                        input_type=input_type,
                        started_at=attempt_started,
                        batch_size=len(texts),
                        input_tokens=input_tokens,
                    )
                    raise error from e
                await self._retry_or_raise(
                    error, attempt, cause=e, input_type=input_type, started_at=attempt_started,
                    batch_size=len(texts), input_tokens=input_tokens,
                )
            except httpx.TimeoutException as e:
                error = EmbeddingRequestError(
                    "Embedding HTTP API timed out",
                    retryable=True,
                    failure_kind="timeout",
                )
                await self._retry_or_raise(
                    error, attempt, cause=e, input_type=input_type, started_at=attempt_started,
                    batch_size=len(texts), input_tokens=input_tokens,
                )
            except (APITimeoutError, APIConnectionError) as e:
                error = EmbeddingRequestError(
                    f"Embedding API connection failed: {e}",
                    retryable=True,
                    failure_kind="connection",
                )
                await self._retry_or_raise(
                    error, attempt, cause=e, input_type=input_type, started_at=attempt_started,
                    batch_size=len(texts), input_tokens=input_tokens,
                )
            except RateLimitError as e:
                retryable = not self._is_quota_error(e)
                error = EmbeddingRequestError(
                    f"Embedding API rate limit failed: {e}",
                    retryable=retryable,
                    failure_kind="rate_limit" if retryable else "quota",
                    provider_status_code=429,
                )
                if not retryable:
                    self._record_terminal_error(
                        error,
                        input_type=input_type,
                        started_at=attempt_started,
                        batch_size=len(texts),
                        input_tokens=input_tokens,
                    )
                    raise error from e
                await self._retry_or_raise(
                    error, attempt, cause=e, input_type=input_type, started_at=attempt_started,
                    batch_size=len(texts), input_tokens=input_tokens,
                )
            except APIStatusError as e:
                retryable = e.status_code in _RETRYABLE_HTTP_STATUS_CODES and not self._is_quota_error(e)
                error = EmbeddingRequestError(
                    f"Embedding API returned {e.status_code}: {e}",
                    retryable=retryable,
                    failure_kind="http_status" if retryable else "validation",
                    provider_status_code=e.status_code,
                )
                if not retryable:
                    self._record_terminal_error(
                        error,
                        input_type=input_type,
                        started_at=attempt_started,
                        batch_size=len(texts),
                        input_tokens=input_tokens,
                    )
                    raise error from e
                await self._retry_or_raise(
                    error, attempt, cause=e, input_type=input_type, started_at=attempt_started,
                    batch_size=len(texts), input_tokens=input_tokens,
                )
        raise AssertionError("embedding retry loop exhausted without a classified error")

    async def _retry_or_raise(
        self,
        error: EmbeddingRequestError,
        attempt: int,
        *,
        cause: Exception | None = None,
        input_type: str = "other",
        started_at: float | None = None,
        batch_size: int | None = None,
        input_tokens: int | None = None,
    ) -> None:
        telemetry = {
            "provider": self.profile.provider,
            "input_type": input_type,
            "duration_seconds": perf_counter() - started_at if started_at is not None else None,
            "batch_size": batch_size,
            "input_tokens": input_tokens,
        }
        if attempt + 1 >= _MAX_RETRIES:
            record_embedding_request(
                status="error", failure_kind=error.failure_kind, retryable=True, **telemetry
            )
            if cause is None:
                raise error
            raise error from cause
        record_embedding_request(
            status="retry", failure_kind=error.failure_kind, retryable=True, **telemetry
        )
        wait = _BASE_BACKOFF * (2 ** attempt)
        logger.warning(
            "Embedding request %s, retrying in %.1fs (attempt %d/%d)",
            error.failure_kind,
            wait,
            attempt + 1,
            _MAX_RETRIES,
        )
        await asyncio.sleep(wait)

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _QUOTA_ERROR_MARKERS)

    def _record_terminal_error(
        self,
        error: EmbeddingRequestError,
        *,
        input_type: str = "other",
        started_at: float | None = None,
        batch_size: int | None = None,
        input_tokens: int | None = None,
    ) -> None:
        record_embedding_request(
            status="error",
            failure_kind=error.failure_kind,
            retryable=error.retryable,
            provider=self.profile.provider,
            input_type=input_type,
            duration_seconds=perf_counter() - started_at if started_at is not None else None,
            batch_size=batch_size,
            input_tokens=input_tokens,
        )

    def _estimated_input_tokens(self, texts: list[str], *, input_type: str) -> int | None:
        if input_type == "image":
            return None
        try:
            encoding = tiktoken.encoding_for_model(self.model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        request_texts = self._apply_input_instruction(texts, input_type=input_type)
        return sum(len(encoding.encode(value)) for value in request_texts)

    async def _embed_local_http(
        self,
        texts: list[str],
        *,
        input_type: str,
        validate_dimensions: bool = True,
    ) -> list[list[float]]:
        request_texts = self._apply_input_instruction(texts, input_type=input_type)
        payload: dict[str, Any] = {
            "inputs": request_texts,
            "normalize": settings.embedding_local_http_normalize,
            "truncate": settings.embedding_local_http_truncate,
        }
        headers = {}
        if settings.embedding_local_http_api_key:
            headers["Authorization"] = f"Bearer {settings.embedding_local_http_api_key}"

        response = await self._local_http_client().post(self.local_http_url, json=payload, headers=headers)
        response.raise_for_status()
        vectors = self._parse_local_http_response(response)
        if len(vectors) != len(texts):
            raise EmbeddingRequestError(
                f"Embedding HTTP API returned {len(vectors)} vectors for {len(texts)} input texts",
                retryable=False,
                failure_kind="validation",
            )
        if validate_dimensions:
            self._validate_dimensions(vectors)
        return vectors

    def _apply_input_instruction(self, texts: list[str], *, input_type: str) -> list[str]:
        if input_type == "query":
            prefix = self.profile.query_instruction
        elif input_type == "document":
            prefix = self.profile.document_instruction
        elif input_type == "image" and self.profile.input_modality == "image":
            prefix = None
        else:
            raise ValueError(
                "embedding input_type must be 'query' or 'document' for text profiles, "
                f"or 'image' for native image profiles; got {input_type!r}"
            )
        if not prefix:
            return texts
        return [f"{prefix}{text}" for text in texts]

    def _parse_local_http_response(self, response: httpx.Response) -> list[list[float]]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingRequestError(
                "Embedding HTTP API returned invalid JSON",
                retryable=False,
                failure_kind="validation",
            ) from exc

        if isinstance(payload, dict) and "embeddings" in payload:
            payload = payload["embeddings"]
        if not isinstance(payload, list) or not all(isinstance(item, list) for item in payload):
            raise EmbeddingRequestError(
                "Embedding HTTP API response must be a list of embedding vectors",
                retryable=False,
                failure_kind="validation",
            )
        return payload

    def _validate_dimensions(self, vectors: list[list[float]]) -> None:
        for index, vector in enumerate(vectors):
            if len(vector) != self.profile.dimensions:
                raise EmbeddingRequestError(
                    f"Embedding dimension mismatch for model {self.model}: "
                    f"chunk {index} returned {len(vector)} dims, expected {self.profile.dimensions}",
                    retryable=False,
                    failure_kind="validation",
                )
