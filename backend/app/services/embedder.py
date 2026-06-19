import asyncio
import logging
from typing import Any

import httpx
from openai import AsyncOpenAI, RateLimitError, APIStatusError

from app.config import settings
from app.embedding_profile import (
    DEFAULT_EMBEDDING_MODEL,
    embedding_request_dimensions,
    resolve_embedding_profile,
)

logger = logging.getLogger(__name__)

_MAX_BATCH = 2048
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0
_RETRYABLE_HTTP_STATUS_CODES = {429, 503}


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
        """Generate embeddings for a batch of texts (up to 2048 per API call)."""
        if self.profile.input_modality == "image":
            raise RuntimeError(
                f"embedding profile {self.profile.profile_name!r} expects image inputs; "
                "use embed_image_references for report-only native image capture instead of text ingestion"
            )
        if not texts:
            return []

        results: list[list[float]] = []
        max_batch_size = min(_MAX_BATCH, self.profile.max_batch_size)
        for batch_start in range(0, len(texts), max_batch_size):
            batch = texts[batch_start: batch_start + max_batch_size]
            embeddings = await self._embed_with_retry(batch, input_type=input_type)
            results.extend(embeddings)
        return results

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
        for attempt in range(_MAX_RETRIES):
            try:
                if self.profile.provider == "local-http":
                    return await self._embed_local_http(
                        texts,
                        input_type=input_type,
                        validate_dimensions=validate_dimensions,
                    )
                request_dimensions = embedding_request_dimensions(self.model)
                request_kwargs = {
                    "model": self.model,
                    "input": texts,
                }
                if request_dimensions is not None:
                    request_kwargs["dimensions"] = request_dimensions
                if self.client is None:
                    raise RuntimeError("OpenAI embedding client is not initialized")
                response = await self.client.embeddings.create(
                    **request_kwargs,
                )
                # Sort by index to preserve order
                vectors = [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
                if validate_dimensions:
                    self._validate_dimensions(vectors)
                return vectors
            except httpx.HTTPStatusError as e:
                if e.response.status_code in _RETRYABLE_HTTP_STATUS_CODES:
                    wait = _BASE_BACKOFF * (2 ** attempt)
                    logger.warning("Embedding HTTP API %d, retrying in %.1fs", e.response.status_code, wait)
                    await asyncio.sleep(wait)
                else:
                    raise RuntimeError(
                        f"Embedding HTTP API returned {e.response.status_code}: {e.response.text[:200]}"
                    ) from e
            except httpx.TimeoutException as e:
                wait = _BASE_BACKOFF * (2 ** attempt)
                logger.warning("Embedding HTTP API timed out, retrying in %.1fs", wait)
                await asyncio.sleep(wait)
                last_timeout = e
            except RateLimitError:
                wait = _BASE_BACKOFF * (2 ** attempt)
                logger.warning("Embedding rate limit hit, retrying in %.1fs (attempt %d/%d)", wait, attempt + 1, _MAX_RETRIES)
                await asyncio.sleep(wait)
            except APIStatusError as e:
                if e.status_code in (429, 503):
                    wait = _BASE_BACKOFF * (2 ** attempt)
                    logger.warning("Embedding API %d, retrying in %.1fs", e.status_code, wait)
                    await asyncio.sleep(wait)
                else:
                    raise
        if "last_timeout" in locals():
            raise RuntimeError(f"Embedding failed after {_MAX_RETRIES} retries due to local HTTP timeout") from last_timeout
        raise RuntimeError(f"Embedding failed after {_MAX_RETRIES} retries")

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
            raise RuntimeError(
                f"Embedding HTTP API returned {len(vectors)} vectors for {len(texts)} input texts"
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
            raise RuntimeError("Embedding HTTP API returned invalid JSON") from exc

        if isinstance(payload, dict) and "embeddings" in payload:
            payload = payload["embeddings"]
        if not isinstance(payload, list) or not all(isinstance(item, list) for item in payload):
            raise RuntimeError("Embedding HTTP API response must be a list of embedding vectors")
        return payload

    def _validate_dimensions(self, vectors: list[list[float]]) -> None:
        for index, vector in enumerate(vectors):
            if len(vector) != self.profile.dimensions:
                raise RuntimeError(
                    f"Embedding dimension mismatch for model {self.model}: "
                    f"chunk {index} returned {len(vector)} dims, expected {self.profile.dimensions}"
                )
