import asyncio
import copy
import json
import logging
import re
from dataclasses import dataclass
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal
from types import SimpleNamespace

import httpx

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
    RateLimitError,
)
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import (
    SUPPORTED_TEXT_MODELS,
    VISION_MODEL,
    active_text_models,
    settings,
    text_model_supports,
)
from app.services.relationship_classification_contract import (
    RELATIONSHIP_PROMPT_VERSION,
    build_relationship_classification_messages,
)
from app.services.image_analysis import (
    VisionAnalysis,
    VisionAnalysisResult,
    VisionProviderMetadata,
    VisionUsage,
)

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4
_BASE_BACKOFF = 2.0

_OPENROUTER_IMMEDIATE_FALLBACK_STATUSES = {401, 403, 404}
_OPENROUTER_BACKOFF_FALLBACK_STATUSES = {429, 503}
_OPENROUTER_TRANSIENT_RETRIES = 2
# The refuseless endpoint reports gateway and upstream faults as plain 5xx, so
# its chain retries those on the same bounded schedule before moving to the next
# configured model. The OpenRouter route keeps its original status set.
_REFUSELESS_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})

# Text-dense diagrams can use most of the bounded 64-entry evidence lists.
# Keep enough room for the provider to close the strict JSON document instead
# of treating a token-limit truncation as malformed output.
_VISION_MAX_TOKENS = 4096
_VISION_TRANSIENT_RETRIES = 2
_VISION_TERMINAL_STATUSES = {401}
_VISION_IMMEDIATE_FALLBACK_STATUSES = {400, 403, 404, 422}

_VISION_PROVIDER_SCHEMA_CONSTRAINTS = {
    "default",
    "maxItems",
    "maxLength",
    "minItems",
    "minLength",
    "title",
}

# Fixed set of image types permitted in an inline vision data URI. The stored
# media type is already derived from verified bytes upstream; this is the final
# allowlist before those bytes leave the host.
_VISION_SAFE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# The gateway is a llama.cpp-style server: it rejects a full JSON-Schema
# response_format ("failed to parse grammar") but accepts JSON-object mode. The
# gateway route therefore requests ``json_object`` and states the field contract
# in the prompt so the model still produces the typed VisionAnalysis shape, which
# the shared tolerant parser then validates.
_GATEWAY_VISION_RESPONSE_FORMAT: dict[str, Any] = {"type": "json_object"}

_GATEWAY_VISION_FIELD_CONTRACT = (
    " Respond with one JSON object with exactly these keys: "
    "summary (string), image_type (string), visible_text (array of strings), "
    "objects (array of strings), entities (array of strings), "
    "relationships (array of objects, each with source, target, direction, label), "
    "visual_details (array of strings), uncertainties (array of strings)."
)


def _safe_vision_media_type(media_type: str) -> str:
    """Return an allowlisted image media type, or fail closed.

    An unknown or parameterised value is rejected rather than forwarded, so the
    data URI can never carry a caller-controlled type into the provider request.
    """
    candidate = (media_type or "").split(";", 1)[0].strip().lower()
    if candidate not in _VISION_SAFE_MEDIA_TYPES:
        raise VisionAnalysisTransportError(
            "Unsupported vision image media type", retryable=False
        )
    return candidate


def _vision_gateway_base_url(raw: str) -> str:
    """Join the gateway root with the OpenAI-compatible ``/v1`` prefix once.

    ``LLM_GATEWAY_URL`` may be given with or without a trailing ``/v1``.
    Appending it blindly would produce a doubled ``/v1/v1`` path, so the suffix
    is added only when it is not already present.
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        raise ValueError("LLM_GATEWAY_URL is not configured")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


# Bound concurrent outbound LLM calls to avoid 429s under parallel ingest gather
_LLM_SEMAPHORE = asyncio.Semaphore(4)


def _ensure_supported_generation_model(model: str) -> str:
    """Return ``model`` only when it belongs to the active provider's catalog.

    Generation resolves through one provider at a time: the selected text
    provider (``LLM_PROVIDER``) owns the whole chain, and each provider accepts
    only its own reviewed models. An unsupported configured value or a
    request-time override raises here instead of silently generating text on a
    different provider.
    """
    cleaned = (model or "").strip()
    catalog = active_text_models(settings.llm_provider)
    if cleaned not in catalog:
        raise ValueError(
            f"unsupported generation model {cleaned!r}; generation for "
            f"LLM_PROVIDER={settings.llm_provider} is restricted to {sorted(catalog)}"
        )
    return cleaned


class _MalformedCompletionResponse(RuntimeError):
    pass


class VisionAnalysisTransportError(RuntimeError):
    """Sanitized vision failure that preserves worker retry classification."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ExtractedEntities(BaseModel):
    people: list[str] = []
    organizations: list[str] = []
    dates: list[str] = []
    key_topics: list[str] = []


class TagExtraction(BaseModel):
    tags: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)


class RelationshipClassification(BaseModel):
    relationship_exists: bool = Field(
        description="True only when Item A has a direct, material semantic relationship to Item B."
    )
    relationship: Literal[
        "related_to",
        "expands_on",
        "contradicts",
        "prerequisite_of",
        "example_of",
        "none",
    ] = Field(description="The directional relationship of Item A to Item B, or none.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the selected relationship label, from 0 to 1.",
    )

    @model_validator(mode="after")
    def validate_relationship_exists(self) -> "RelationshipClassification":
        if self.relationship_exists != (self.relationship != "none"):
            raise ValueError("relationship_exists must agree with relationship")
        return self


@dataclass(frozen=True)
class LLMGenerationSettings:
    """Explicit sampling identity for reproducible bounded evaluations."""

    max_tokens: int = 1024
    temperature: float | None = None
    seed: int | None = None

    def request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.seed is not None:
            kwargs["seed"] = self.seed
        return kwargs


@dataclass
class LLMCompletionDiagnostics:
    """Bounded, non-sensitive completion metadata for operational callers."""

    provider: Literal["openrouter", "refuseless", "openai", "unknown"] = "unknown"
    upstream_provider: str = "unknown"
    requested_model: str = "unknown"
    actual_model: str = "unknown"
    temperature: float | None = None
    seed: int | None = None
    fallback_used: bool = False
    retry_count: int = 0


@dataclass(frozen=True)
class RelationshipClassificationResult:
    relationship: str
    confidence: float
    validation_outcome: Literal["valid", "empty", "malformed", "timeout", "provider_error"]
    provider: Literal["openrouter", "refuseless", "openai", "unknown"]
    upstream_provider: str
    requested_model: str
    model: str
    retry_provider: Literal["openrouter", "refuseless", "openai", "unknown"]
    fallback_used: bool
    retry_count: int
    prompt_version: str
    temperature: float | None
    seed: int | None


class BrowserAction(BaseModel):
    action: str
    text: str


class BrowserActions(BaseModel):
    actions: list[BrowserAction] = Field(default_factory=list)


def _reasoning_extra_body(model: str) -> dict[str, Any]:
    # Advertised reasoning support does not imply it can be disabled. Flash
    # requires reasoning; leave its provider default untouched on every path.
    if text_model_supports(model, "disable_reasoning"):
        return {"reasoning": {"enabled": False}}
    return {}


def _strip_reasoning_blocks(raw: str) -> str:
    return re.sub(r"<think\b[^>]*>.*?</think>", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()


def _strip_code_fence(raw: str) -> str:
    clean = raw.strip()
    if not clean.startswith("```"):
        return clean
    first_newline = clean.find("\n")
    if first_newline == -1:
        return clean.strip("`").strip()
    return clean[first_newline + 1 :].rsplit("```", 1)[0].strip()


def _extract_balanced_json(raw: str) -> str:
    clean = _strip_code_fence(_strip_reasoning_blocks(raw))
    if clean.startswith("{") or clean.startswith("["):
        return clean

    start_candidates = [idx for idx in (clean.find("{"), clean.find("[")) if idx != -1]
    if not start_candidates:
        raise ValueError("LLM response did not contain JSON")
    start = min(start_candidates)
    opening = clean[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(clean)):
        char = clean[index]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return clean[start : index + 1]
    raise ValueError("LLM response JSON was not balanced")


def _json_loads_from_response(raw: str) -> Any:
    return json.loads(_extract_balanced_json(raw))


def _normalize_terms(values: list[str], *, limit: int) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = str(value).strip().lower()
        if not term or term in seen:
            continue
        seen.add(term)
        normalized.append(term)
        if len(normalized) >= limit:
            break
    return normalized


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert Pydantic JSON Schema into the strict subset accepted by providers."""
    strict_schema = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
                for child in properties.values():
                    visit(child)
            node["additionalProperties"] = False
        for key in ("$defs", "definitions"):
            nested = node.get(key)
            if isinstance(nested, dict):
                for child in nested.values():
                    visit(child)
        for key in ("items", "anyOf", "oneOf", "allOf"):
            child = node.get(key)
            if isinstance(child, list):
                for entry in child:
                    visit(entry)
            else:
                visit(child)

    visit(strict_schema)
    return strict_schema


def _vision_provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove serving constraints while retaining strict local validation.

    Gemini converts JSON Schema constraints into a serving grammar. Pydantic's
    bounded list and string constraints can make that grammar too large. The
    worker still validates the provider response against ``VisionAnalysis``, so
    removing these provider-side bounds does not weaken the persisted contract.
    """
    provider_schema = _strict_json_schema(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in _VISION_PROVIDER_SCHEMA_CONSTRAINTS:
                node.pop(key, None)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(provider_schema)
    return provider_schema


@dataclass(frozen=True)
class _VisionRoute:
    """Resolved vision transport: one client, its model chain, and its provider.

    The OpenRouter route keeps its multi-model chain and provider-side filtering;
    the gateway route is a single dedicated model with no cross-provider hop.
    """

    client: Any
    models: list[str]
    provider: Literal["openrouter", "gateway"]
    openrouter_extras: bool


class LLMService:
    """Text generation through the configured provider.

    ``LLM_PROVIDER=refuseless`` (default) sends every generation call to the
    [OI]-compatible endpoint at ``LLM_BASE_URL`` with ``LLM_API_KEY`` and the
    reviewed refuseless catalog. ``LLM_PROVIDER=openrouter`` keeps the
    MiniMax-only OpenRouter route. Only one provider's client is ever
    constructed; an unsupported model fails visibly here rather than falling
    through to the other provider. [OI] is used only for embeddings and
    transcription, never for generation.
    """

    def __init__(self, *, vision_transport: httpx.AsyncBaseTransport | None = None):
        self._vision_transport = vision_transport
        self.provider: Literal["openrouter", "refuseless"] = settings.llm_provider
        # Fail closed here rather than at config import: only a process that
        # actually calls the model needs the key. The migration job imports the
        # config module for the database URL and must be able to run before the
        # ExternalSecret carrying the key is applied.
        settings.require_llm_credentials()
        if self.provider == "refuseless":
            # Same client type and timeout behavior as the OpenRouter route, but
            # with the refuseless credential and base URL only. No OpenRouter
            # header or credential is attached.
            self.client = AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
            )
            self.default_model = settings.llm_default_model
            self._fallback_models = [
                m.strip()
                for m in settings.llm_fallback_models.split(",")
                if m.strip()
            ]
        else:
            self.client = AsyncOpenAI(
                api_key=settings.openrouter_api_key,
                base_url="https://openrouter.ai/api/v1",
                default_headers={
                    "HTTP-Referer": "https://github.com/palaceoftruth/palaceoftruth",
                    "X-Title": "Palace of Truth",
                },
            )
            self.default_model = settings.openrouter_default_model
            self._fallback_models = [
                m.strip()
                for m in settings.openrouter_fallback_models.split(",")
                if m.strip()
            ]
        # Self-hosted vision gateway client. Built lazily and only when the
        # gateway vision route is selected, so an OpenRouter deployment never
        # constructs an object that holds the gateway credential.
        self._vision_gateway_client: AsyncOpenAI | None = None

    def _build_model_chain(self, model: str | None) -> list[str]:
        chain = [model or self.default_model] + self._fallback_models
        return [_ensure_supported_generation_model(candidate) for candidate in chain]

    @staticmethod
    def _get_openrouter_fallback_delay(status: int, attempt: int) -> float | None:
        # 401/403/404 usually mean the current model is not usable for this key,
        # so move on to the next configured model immediately instead of waiting.
        if status in _OPENROUTER_IMMEDIATE_FALLBACK_STATUSES:
            return 0.0
        if status in _OPENROUTER_BACKOFF_FALLBACK_STATUSES:
            return min(_BASE_BACKOFF * (2 ** attempt), 16.0)
        return None

    def _transient_statuses(self) -> frozenset[int]:
        """Statuses that earn a bounded same-model retry on this provider."""

        if self.provider == "refuseless":
            return frozenset(_OPENROUTER_BACKOFF_FALLBACK_STATUSES) | _REFUSELESS_TRANSIENT_STATUSES
        return frozenset(_OPENROUTER_BACKOFF_FALLBACK_STATUSES)

    def _get_fallback_delay(self, status: int, attempt: int) -> float | None:
        """Delay before the next configured model, or None to fail closed."""

        if self.provider == "refuseless" and status in _REFUSELESS_TRANSIENT_STATUSES:
            return min(_BASE_BACKOFF * (2 ** attempt), 16.0)
        return self._get_openrouter_fallback_delay(status, attempt)

    @staticmethod
    def _get_openrouter_transient_retry_delay(retry: int) -> float:
        return min(_BASE_BACKOFF * (2 ** retry), 16.0)

    @staticmethod
    def _get_usage_payload(response: Any) -> dict | None:
        if response.usage is None:
            return None
        return {
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
        }

    @staticmethod
    def _record_completion_identity(
        diagnostics: LLMCompletionDiagnostics | None,
        response: Any,
        *,
        fallback_model: str,
    ) -> None:
        if diagnostics is None:
            return
        diagnostics.actual_model = str(getattr(response, "model", None) or "unknown")
        diagnostics.upstream_provider = str(getattr(response, "provider", None) or "unknown")

    @staticmethod
    def _completion_content(response: Any) -> str:
        choices = getattr(response, "choices", None)
        if not choices:
            provider_error = getattr(response, "error", None)
            if isinstance(provider_error, dict):
                code = provider_error.get("code", "unknown")
                message = provider_error.get("message", "unknown provider error")
                raise _MalformedCompletionResponse(
                    f"completion response did not include choices (provider error {code}: {message})"
                )
            raise _MalformedCompletionResponse("completion response did not include choices")
        try:
            content = choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise _MalformedCompletionResponse("completion response had an invalid first choice") from exc
        if content is None:
            raise _MalformedCompletionResponse("completion response first choice did not include message content")
        if not isinstance(content, str):
            raise _MalformedCompletionResponse("completion response message content was not text")
        return content

    async def _create_text_completion(
        self,
        current_model: str,
        messages: list[dict],
        response_format: dict[str, Any] | None = None,
        generation_settings: LLMGenerationSettings | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> Any:
        generation_settings = generation_settings or LLMGenerationSettings()
        request_kwargs = generation_settings.request_kwargs()
        if "seed" in request_kwargs and not text_model_supports(current_model, "seed"):
            # Flash does not advertise seed; keep requests within advertised
            # capabilities rather than assuming the provider accepts it.
            request_kwargs.pop("seed")
            logger.warning(
                "Model %s does not advertise sampling seed support; omitting seed",
                current_model,
            )
        kwargs: dict[str, Any] = {
            "model": current_model,
            "messages": messages,
            **request_kwargs,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
            if self.provider == "openrouter":
                # OpenRouter-only routing hint: require an upstream that honors
                # the requested parameters. Refuseless never receives it.
                kwargs["extra_body"] = {"provider": {"require_parameters": True}}
        if extra_body:
            kwargs["extra_body"] = {
                **kwargs.get("extra_body", {}),
                **extra_body,
            }
        # Preserve bounded visible-output budgets for M3/M2.7, but never send
        # a reasoning override to Flash, even if a caller supplied one.
        kwargs.get("extra_body", {}).pop("reasoning", None)
        kwargs["extra_body"] = {
            **kwargs.get("extra_body", {}),
            **_reasoning_extra_body(current_model),
        }
        if not kwargs["extra_body"]:
            kwargs.pop("extra_body")
        async with _LLM_SEMAPHORE:
            return await self.client.chat.completions.create(**kwargs)

    async def _get_vision_gateway_client(self) -> AsyncOpenAI:
        """Build the gateway client on first use, carrying only the gateway key."""
        if self._vision_gateway_client is None:
            self._vision_gateway_client = AsyncOpenAI(
                # The gateway credential, never the OpenAI or OpenRouter key.
                api_key=settings.llm_gateway_token,
                base_url=_vision_gateway_base_url(settings.llm_gateway_url),
                timeout=settings.vision_gateway_timeout_seconds,
                # The vision chain already applies its own bounded retries; keep
                # the SDK from adding hidden ones onto a shared gateway.
                max_retries=0,
            )
        return self._vision_gateway_client

    async def _build_vision_route(self) -> _VisionRoute:
        """Resolve the configured vision transport, or fail closed before any request."""
        if settings.vision_provider == "gateway":
            if not settings.llm_gateway_url.strip():
                raise VisionAnalysisTransportError(
                    "Vision gateway URL is not configured", retryable=False
                )
            if not settings.llm_gateway_token.strip():
                raise VisionAnalysisTransportError(
                    "Vision gateway credential is not configured", retryable=False
                )
            return _VisionRoute(
                client=await self._get_vision_gateway_client(),
                models=[settings.vision_gateway_model],
                provider="gateway",
                openrouter_extras=False,
            )
        models = self._build_vision_model_chain()
        if not models:
            raise VisionAnalysisTransportError(
                "Vision analysis has no configured OpenRouter model",
                retryable=False,
            )
        return _VisionRoute(
            client=self.client,
            models=models,
            provider="openrouter",
            openrouter_extras=True,
        )

    async def _create_vision_completion(
        self,
        route: _VisionRoute,
        current_model: str,
        messages: list[dict],
        response_format: dict[str, Any] | None,
        generation_settings: LLMGenerationSettings,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": current_model,
            "messages": messages,
            **generation_settings.request_kwargs(),
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if route.openrouter_extras:
            # Cloud vision is M3-only; keep its bounded visible-output budget.
            # Gateway requests never receive OpenRouter-specific parameters.
            kwargs["extra_body"] = {
                "provider": {"require_parameters": True},
                **_reasoning_extra_body(current_model),
            }
        async with _LLM_SEMAPHORE:
            return await route.client.chat.completions.create(**kwargs)

    async def aclose(self) -> None:
        """Close every outbound client this service owns."""
        for client in (self.client, self._vision_gateway_client):
            if client is None:
                continue
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:  # pragma: no cover - defensive shutdown path
                logger.debug("failed to close an LLM client", exc_info=True)
        self._vision_gateway_client = None

    async def _complete_with_fallback(
        self,
        messages: list[dict],
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
        diagnostics: LLMCompletionDiagnostics | None = None,
        generation_settings: LLMGenerationSettings | None = None,
        allow_model_fallbacks: bool = True,
        response_format_for_model: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> Any:
        generation_settings = generation_settings or LLMGenerationSettings()
        if allow_model_fallbacks:
            chain = self._build_model_chain(model)
        else:
            chain = [_ensure_supported_generation_model(model or self.default_model)]
        if diagnostics is not None:
            diagnostics.requested_model = chain[0]
            diagnostics.temperature = generation_settings.temperature
        for attempt, current_model in enumerate(chain):
            if diagnostics is not None:
                # Track the effective seed even when fallback changes model
                # capabilities; Flash never receives one.
                diagnostics.seed = (
                    generation_settings.seed
                    if text_model_supports(current_model, "seed")
                    else None
                )
            attempt_response_format = (
                response_format_for_model(current_model)
                if response_format_for_model is not None
                else response_format
            )
            transient_retry = 0
            while True:
                try:
                    if diagnostics is not None:
                        diagnostics.provider = self.provider
                    response = await self._create_text_completion(
                        current_model,
                        messages,
                        response_format=attempt_response_format,
                        generation_settings=generation_settings,
                    )
                    self._completion_content(response)
                    self._record_completion_identity(
                        diagnostics,
                        response,
                        fallback_model=current_model,
                    )
                    return response
                except _MalformedCompletionResponse as exc:
                    if transient_retry < _OPENROUTER_TRANSIENT_RETRIES:
                        delay = self._get_openrouter_transient_retry_delay(transient_retry)
                        logger.warning(
                            "LLM %s returned malformed completion (%s), retrying same model in %.1fs",
                            current_model,
                            exc,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        transient_retry += 1
                        if diagnostics is not None:
                            diagnostics.retry_count += 1
                        continue
                    if attempt < len(chain) - 1:
                        next_model = chain[attempt + 1]
                        logger.warning(
                            "LLM %s returned malformed completion after %d retries (%s), trying %s",
                            current_model,
                            transient_retry,
                            exc,
                            next_model,
                        )
                        if diagnostics is not None:
                            diagnostics.fallback_used = True
                        break
                    if not allow_model_fallbacks:
                        logger.warning(
                            "Calibrated model %s returned malformed completion after %d retries; failing closed",
                            current_model,
                            transient_retry,
                        )
                        raise RuntimeError(
                            f"calibrated model {current_model} returned malformed completions"
                        ) from exc
                    logger.warning(
                        "Generation chain exhausted: %s returned malformed completion "
                        "after %d retries; failing closed",
                        current_model,
                        transient_retry,
                    )
                    raise RuntimeError(
                        f"generation chain exhausted: {current_model} "
                        "returned malformed completions"
                    ) from exc
                except (RateLimitError, APIStatusError) as e:
                    status = getattr(e, "status_code", 429)
                    if (
                        status in self._transient_statuses()
                        and transient_retry < _OPENROUTER_TRANSIENT_RETRIES
                    ):
                        delay = self._get_openrouter_transient_retry_delay(transient_retry)
                        logger.warning(
                            "LLM %s returned %d, retrying same model in %.1fs",
                            current_model,
                            status,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        transient_retry += 1
                        if diagnostics is not None:
                            diagnostics.retry_count += 1
                        continue
                    delay = self._get_fallback_delay(status, attempt)
                    if delay is not None and attempt < len(chain) - 1:
                        next_model = chain[attempt + 1]
                        logger.warning(
                            "LLM %s returned %d after %d retries, trying %s in %.1fs",
                            current_model,
                            status,
                            transient_retry,
                            next_model,
                            delay,
                        )
                        if delay:
                            await asyncio.sleep(delay)
                        if diagnostics is not None:
                            diagnostics.fallback_used = True
                        break
                    if not allow_model_fallbacks:
                        logger.warning(
                            "Calibrated model %s failed with status %d; failing closed",
                            current_model,
                            status,
                        )
                        raise RuntimeError(
                            f"calibrated model {current_model} failed with status {status}"
                        ) from e
                    logger.warning(
                        "Generation chain exhausted (%s %d); failing closed",
                        current_model,
                        status,
                    )
                    raise RuntimeError(
                        f"generation chain exhausted: {current_model} "
                        f"failed with status {status}"
                    ) from e
        raise RuntimeError("LLM completion failed after all retries and fallbacks")

    async def complete(
        self,
        messages: list[dict],
        model: str | None = None,
        *,
        diagnostics: LLMCompletionDiagnostics | None = None,
        generation_settings: LLMGenerationSettings | None = None,
        allow_model_fallbacks: bool = True,
    ) -> str:
        """Send chat completion with automatic fallback through the model chain.

        Order: configured primary model, then its configured fallbacks. The
        chain is bounded to the active provider's reviewed catalog; an
        exhausted chain fails closed instead of falling through to another
        provider.
        """
        response = await self._complete_with_fallback(
            messages,
            model=model,
            diagnostics=diagnostics,
            generation_settings=generation_settings,
            allow_model_fallbacks=allow_model_fallbacks,
        )
        return self._completion_content(response)

    async def complete_structured(
        self,
        messages: list[dict],
        schema: type[BaseModel],
        *,
        schema_name: str,
        model: str | None = None,
        diagnostics: LLMCompletionDiagnostics | None = None,
        generation_settings: LLMGenerationSettings | None = None,
        allow_model_fallbacks: bool = True,
    ) -> BaseModel:
        """Request schema-shaped JSON, with one legacy-parser retry.

        Models that advertise strict structured outputs receive the full
        ``json_schema`` response format. Catalog entries that only advertise
        ``response_format`` (MiniMax M2.7/Flash) instead receive plain ``json_object``
        and are validated locally by the same Pydantic schema. In both cases the
        existing legacy JSON retry stays the final boundary, so a provider that
        ignores the requested shape cannot bypass typed validation.
        """

        def _response_format_for(model: str) -> dict[str, Any] | None:
            if text_model_supports(model, "structured_outputs"):
                return {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": _strict_json_schema(schema.model_json_schema()),
                    },
                }
            if text_model_supports(model, "response_format"):
                logger.warning(
                    "Model %s does not advertise structured outputs; requesting "
                    "json_object for %s and enforcing the typed schema",
                    model,
                    schema_name,
                )
                return {"type": "json_object"}
            logger.warning(
                "Model %s does not advertise response_format; relying on prompt "
                "plus typed validation for %s",
                model,
                schema_name,
            )
            return None

        try:
            response = await self._complete_with_fallback(
                messages,
                model=model,
                diagnostics=diagnostics,
                generation_settings=generation_settings,
                allow_model_fallbacks=allow_model_fallbacks,
                response_format_for_model=_response_format_for,
            )
            return schema.model_validate(_json_loads_from_response(self._completion_content(response)))
        except Exception as exc:
            logger.warning("Structured LLM response failed for %s; retrying legacy JSON parse: %s", schema_name, exc)
            if diagnostics is not None:
                diagnostics.retry_count += 1
            raw = await self.complete(
                messages,
                model=model,
                diagnostics=diagnostics,
                generation_settings=generation_settings,
                allow_model_fallbacks=allow_model_fallbacks,
            )
            return schema.model_validate(_json_loads_from_response(raw))

    async def complete_with_usage(
        self, messages: list[dict], model: str | None = None
    ) -> tuple[str, dict | None]:
        """Like complete(), but also returns usage data.

        Returns (content, {"input_tokens": N, "output_tokens": M}) or (content, None).
        Usage is None when the provider does not return usage data or on fallback paths
        where usage is unavailable.
        """
        response = await self._complete_with_fallback(messages, model=model)
        content = self._completion_content(response)
        return content, self._get_usage_payload(response)

    async def stream_complete(
        self,
        messages: list[dict],
        model: str | None = None,
    ) -> tuple[AsyncIterator[str], Callable[[], dict | None]]:
        """Return (token_stream, get_usage).

        Iterate the token_stream to receive text chunks. After the stream is exhausted,
        call get_usage() to retrieve {"input_tokens": N, "output_tokens": M} or None.
        Falls back to complete() (the full bounded provider retry) on streaming errors.
        An unsupported model override fails before any request is made.

        Note: some OpenRouter upstreams (e.g. xAI/Grok) reject stream_options — on
        BadRequestError containing "stream_options", the stream is retried without usage
        tracking and get_usage() returns None.
        """
        resolved_model = _ensure_supported_generation_model(model or self.default_model)
        usage_store: dict[str, dict | None] = {"value": None}

        async def _stream_inner() -> AsyncIterator[str]:
            try:
                async with _LLM_SEMAPHORE:
                    stream = await self.client.chat.completions.create(
                        model=resolved_model,
                        messages=messages,
                        max_tokens=1024,
                        stream=True,
                        stream_options={"include_usage": True},
                        extra_body=_reasoning_extra_body(resolved_model),
                    )
                async for chunk in stream:
                    choices = chunk.choices or []
                    # Final usage-only chunk: no choices, usage populated.
                    # Must check both conditions — some providers set usage on content chunks.
                    if len(choices) == 0 and chunk.usage is not None:
                        usage_store["value"] = {
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                        }
                        continue
                    if choices:
                        delta = choices[0].delta.content
                        if delta:
                            yield delta
            except BadRequestError as exc:
                # Some providers reject stream_options — retry without usage tracking
                if "stream_options" in str(exc).lower():
                    logger.warning("Provider rejected stream_options; retrying stream without usage tracking")
                    try:
                        async with _LLM_SEMAPHORE:
                            stream = await self.client.chat.completions.create(
                                model=resolved_model,
                                messages=messages,
                                max_tokens=1024,
                                stream=True,
                                extra_body=_reasoning_extra_body(resolved_model),
                            )
                        async for chunk in stream:
                            choices = chunk.choices or []
                            if choices:
                                delta = choices[0].delta.content
                                if delta:
                                    yield delta
                        return
                    except Exception as retry_exc:
                        logger.warning("Stream retry without stream_options also failed: %s", retry_exc)
                # Fall through to complete() fallback
                logger.warning("Streaming failed (BadRequestError), falling back to complete()")
                try:
                    response = await self.complete(messages, model=model)
                    if response:
                        yield response
                except Exception as fallback_exc:
                    logger.error("Fallback complete() also failed: %s", fallback_exc)
                    raise
            except Exception as exc:
                logger.warning("Streaming failed (%s), falling back to complete(): %s", type(exc).__name__, exc)
                try:
                    response = await self.complete(messages, model=model)
                    if response:
                        yield response
                except Exception as fallback_exc:
                    logger.error("Fallback complete() also failed: %s", fallback_exc)
                    raise

        def get_usage() -> dict | None:
            return usage_store["value"]

        return _stream_inner(), get_usage

    async def summarize(
        self,
        text: str,
        model: str | None = None,
        *,
        diagnostics: LLMCompletionDiagnostics | None = None,
    ) -> str:
        """Generate a 2-3 sentence summary of the provided text."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a concise summarizer. Given a piece of text, produce a "
                    "2-3 sentence summary that captures the main ideas. "
                    "Respond with only the summary, no preamble."
                ),
            },
            {"role": "user", "content": f"Summarize this text:\n\n{text}"},
        ]
        return await self.complete(messages, model=model, diagnostics=diagnostics)

    async def classify_relationship(
        self,
        title_a: str,
        summary_a: str,
        title_b: str,
        summary_b: str,
    ) -> tuple[str, float]:
        """Classify the relationship of item A to item B.

        Returns (relationship_type, confidence). On parse failure returns ("none", 0.0).
        """
        result = await self.classify_relationship_detailed(title_a, summary_a, title_b, summary_b)
        return result.relationship, result.confidence

    async def classify_relationship_detailed(
        self,
        title_a: str,
        summary_a: str,
        title_b: str,
        summary_b: str,
    ) -> RelationshipClassificationResult:
        """Classify a relationship with bounded diagnostics for worker telemetry.

        Diagnostics intentionally exclude prompts, item identifiers, and provider messages
        so metrics and logs remain safe for tenant content.
        """
        messages = build_relationship_classification_messages(
            title_a,
            summary_a,
            title_b,
            summary_b,
        )
        generation_settings = LLMGenerationSettings(
            max_tokens=256,
            temperature=settings.relationship_classification_temperature,
            seed=settings.relationship_classification_seed,
        )
        diagnostics = LLMCompletionDiagnostics()
        try:
            data = await self.complete_structured(
                messages,
                RelationshipClassification,
                schema_name="relationship_classification_v3",
                model=settings.relationship_classification_model,
                diagnostics=diagnostics,
                generation_settings=generation_settings,
                allow_model_fallbacks=False,
            )
            assert isinstance(data, RelationshipClassification)
            outcome: Literal["valid", "empty", "malformed", "timeout", "provider_error"] = (
                "empty" if data.relationship == "none" else "valid"
            )
            return RelationshipClassificationResult(
                relationship=data.relationship,
                confidence=data.confidence,
                validation_outcome=outcome,
                provider=diagnostics.provider,
                upstream_provider=diagnostics.upstream_provider,
                requested_model=diagnostics.requested_model,
                model=diagnostics.actual_model,
                # Current bounded retry loops execute against OpenRouter models;
                # direct OpenAI is a terminal fallback, never a retry target.
                # Every bounded retry runs against the same configured
                # provider; there is no cross-provider retry target.
                retry_provider=diagnostics.provider,
                fallback_used=diagnostics.fallback_used,
                retry_count=diagnostics.retry_count,
                prompt_version=RELATIONSHIP_PROMPT_VERSION,
                temperature=diagnostics.temperature,
                seed=diagnostics.seed,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            logger.warning("Relationship classification timed out: %s", exc)
            outcome = "timeout"
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Relationship classification was malformed: %s", exc)
            outcome = "malformed"
        except Exception as exc:
            logger.warning("Failed to classify relationship: %s", exc)
            outcome = "provider_error"
        return RelationshipClassificationResult(
            relationship="none",
            confidence=0.0,
            validation_outcome=outcome,
            provider=diagnostics.provider,
            upstream_provider=diagnostics.upstream_provider,
            requested_model=diagnostics.requested_model,
            model=diagnostics.actual_model,
            retry_provider=diagnostics.provider,
            fallback_used=diagnostics.fallback_used,
            retry_count=diagnostics.retry_count,
            prompt_version=RELATIONSHIP_PROMPT_VERSION,
            temperature=diagnostics.temperature,
            seed=diagnostics.seed,
        )

    async def get_browser_actions(self, page_text: str, url: str) -> list[dict]:
        """Given visible page text, return a list of click actions to dismiss overlays.

        Returns a list like [{"action": "click", "text": "Accept all"}].
        Returns an empty list if no interaction is needed.
        Used by the browser scraper to get past cookie banners, age gates, etc.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "You help automate web scraping. Given the visible text of a webpage, "
                    "identify any overlays, cookie consent dialogs, age gates, login walls, "
                    "or other barriers that are blocking the main content. "
                    "Return ONLY a JSON array of click actions needed to dismiss them. "
                    'Each action: {"action": "click", "text": "<exact visible button text>"}. '
                    "If there are no barriers, return an empty array: []"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"URL: {url}\n\n"
                    f"Visible page text (first 2000 chars):\n{page_text[:2000]}\n\n"
                    "What should I click to access the main content? Return JSON only."
                ),
            },
        ]
        raw = ""
        try:
            raw = await self.complete(messages)
            parsed = _json_loads_from_response(raw)
            if isinstance(parsed, list):
                return [
                    {"action": action.action, "text": action.text}
                    for action in (BrowserAction.model_validate(item) for item in parsed)
                    if action.action == "click"
                ]
            actions = BrowserActions.model_validate(parsed).actions
            return [{"action": action.action, "text": action.text} for action in actions if action.action == "click"]
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("Failed to parse browser actions from LLM: %r", raw)
        return []

    @staticmethod
    def _vision_response_format() -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "vision_analysis_v1",
                "strict": True,
                "schema": _vision_provider_schema(VisionAnalysis.model_json_schema()),
            },
        }

    def _build_vision_model_chain(self) -> list[str]:
        configured = [settings.openrouter_vision_model]
        configured.extend(
            model.strip()
            for model in settings.openrouter_vision_fallback_models.split(",")
            if model.strip()
        )
        # Keep a malformed configuration from causing duplicate requests while
        # preserving the configured order and the dedicated vision boundary.
        if settings.openrouter_vision_model != VISION_MODEL or settings.openrouter_vision_fallback_models.strip():
            raise ValueError("Cloud vision requires MiniMax M3 only, without fallbacks")
        return [VISION_MODEL]

    @staticmethod
    def _sanitize_vision_usage(response: Any) -> VisionUsage | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None

        values: dict[str, int] = {}
        for output_name, input_name in (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            value = getattr(usage, input_name, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values[output_name] = value
        if not values:
            return None
        try:
            return VisionUsage.model_validate(values)
        except ValidationError:
            return None

    @staticmethod
    def _sanitize_returned_model(response: Any) -> str:
        returned_model = getattr(response, "model", None)
        if not isinstance(returned_model, str):
            return "unknown"
        returned_model = returned_model.strip()
        return returned_model[:200] or "unknown"

    @classmethod
    def _parse_vision_response(
        cls,
        response: Any,
        requested_model: str,
        *,
        provider: Literal["openrouter", "gateway", "custom"] = "openrouter",
    ) -> VisionAnalysisResult:
        content = cls._completion_content(response).strip()
        if not content:
            raise ValueError("vision response was empty")
        analysis = VisionAnalysis.model_validate(_json_loads_from_response(content))
        return VisionAnalysisResult(
            analysis=analysis,
            provider=VisionProviderMetadata(
                provider=provider,
                requested_model=requested_model,
                returned_model=cls._sanitize_returned_model(response),
                usage=cls._sanitize_vision_usage(response),
            ),
        )

    @staticmethod
    def _vision_status_code(exc: APIStatusError) -> int | None:
        status = getattr(exc, "status_code", None)
        return status if isinstance(status, int) else None

    async def _complete_vision_with_fallback(
        self,
        messages: list[dict],
        *,
        route: _VisionRoute,
    ) -> VisionAnalysisResult:
        models = route.models

        response_format = (
            _GATEWAY_VISION_RESPONSE_FORMAT
            if route.provider == "gateway"
            else self._vision_response_format()
        )
        generation_settings = LLMGenerationSettings(
            max_tokens=_VISION_MAX_TOKENS,
            temperature=0.0,
        )
        last_failure = "unavailable"
        last_status_code: int | None = None
        for model_index, current_model in enumerate(models):
            transient_retry = 0
            while True:
                try:
                    response = await self._create_vision_completion(
                        route,
                        current_model,
                        messages,
                        response_format,
                        generation_settings,
                    )
                    return self._parse_vision_response(
                        response, current_model, provider=route.provider
                    )
                except (ValidationError, json.JSONDecodeError, ValueError, _MalformedCompletionResponse):
                    # A provider response is never copied into logs or returned
                    # to callers. Try the next configured model immediately.
                    last_failure = "invalid_output"
                    # image_analysis.py treats a 4xx status as permanent; use
                    # a synthetic sanitized status because malformed output
                    # has no provider status of its own.
                    last_status_code = 422
                    logger.warning("Vision model %s returned invalid structured output", current_model)
                    break
                except APIStatusError as exc:
                    status = self._vision_status_code(exc)
                    if status in _VISION_TERMINAL_STATUSES:
                        logger.error("Vision provider authentication failed for model %s", current_model)
                        raise VisionAnalysisTransportError(
                            "Vision provider authentication failed",
                            retryable=False,
                            status_code=status,
                        ) from exc
                    if status == 429 or (status is not None and status >= 500):
                        last_failure = "unavailable"
                        last_status_code = status
                        if transient_retry < _VISION_TRANSIENT_RETRIES:
                            delay = self._get_openrouter_transient_retry_delay(transient_retry)
                            logger.warning(
                                "Vision model %s returned transient status %s; retry %d in %.1fs",
                                current_model,
                                status,
                                transient_retry + 1,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            transient_retry += 1
                            continue
                    else:
                        last_failure = "rejected"
                        last_status_code = status or 400
                    break
                except (APIConnectionError, APITimeoutError, asyncio.TimeoutError, TimeoutError, OSError):
                    last_failure = "unavailable"
                    last_status_code = None
                    if transient_retry < _VISION_TRANSIENT_RETRIES:
                        delay = self._get_openrouter_transient_retry_delay(transient_retry)
                        logger.warning(
                            "Vision model %s had a transient network failure; retry %d in %.1fs",
                            current_model,
                            transient_retry + 1,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        transient_retry += 1
                        continue
                    break
                except Exception:
                    # Unknown provider failures are treated as an immediate
                    # model fallback. The exception body may contain provider
                    # data and must not be logged or surfaced.
                    last_failure = "unavailable"
                    logger.warning("Vision model %s failed with an unclassified provider error", current_model)
                    break

            if model_index < len(models) - 1:
                logger.warning(
                    "Vision model %s exhausted; trying configured fallback %s",
                    current_model,
                    models[model_index + 1],
                )

        if last_failure == "invalid_output":
            raise VisionAnalysisTransportError(
                "Vision provider returned invalid structured output",
                retryable=False,
                status_code=422,
            )
        if last_failure == "rejected":
            raise VisionAnalysisTransportError(
                "Vision provider rejected the image analysis request",
                retryable=False,
                status_code=last_status_code or 400,
            )
        raise VisionAnalysisTransportError(
            "Vision provider unavailable after bounded retries",
            retryable=True,
            status_code=last_status_code,
        )

    async def _complete_custom_vision(self, messages: list[dict]) -> VisionAnalysisResult:
        """Dedicated endpoint; one schema-unsupported retry, no cloud escape.

        Avoid SDK auth defaults, ambient proxies/netrc and redirects. Neither
        provider error bodies nor endpoint/credential/payload enter exceptions.
        """
        headers = {"Content-Type": "application/json"}
        if settings.vision_api_key:
            headers["Authorization"] = f"Bearer {settings.vision_api_key}"
        body = {
            "model": settings.vision_model,
            "messages": messages,
            "max_tokens": _VISION_MAX_TOKENS,
            "temperature": 0.0,
            "response_format": self._vision_response_format(),
        }
        url = settings.vision_base_url.rstrip("/") + "/chat/completions"
        async with httpx.AsyncClient(
            transport=self._vision_transport,
            timeout=httpx.Timeout(settings.vision_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for attempt in range(2):
                try:
                    async with _LLM_SEMAPHORE:
                        response = await client.post(url, headers=headers, json=body)
                except httpx.RequestError:
                    raise VisionAnalysisTransportError(
                        "Custom vision provider unavailable", retryable=True
                    ) from None
                status = response.status_code
                if status >= 300:
                    # Inspect only to classify explicit schema/format rejection.
                    # Never expose or log this untrusted response body.
                    message = response.text[:4096].lower()
                    schema_unsupported = (
                        status in {400, 422}
                        and any(word in message for word in ("response_format", "json_schema"))
                        and any(word in message for word in ("unsupported", "not supported", "unknown", "unrecognized"))
                    )
                    if attempt == 0 and schema_unsupported:
                        body.pop("response_format")
                        continue
                    raise VisionAnalysisTransportError(
                        "Custom vision provider rejected request" if status < 500 and status != 429
                        else "Custom vision provider unavailable",
                        retryable=status == 429 or status >= 500,
                        status_code=status,
                    ) from None
                try:
                    data = response.json()
                    # Share bounded parsing/validation, never text generation.
                    completion = json.loads(json.dumps(data), object_hook=lambda obj: SimpleNamespace(**obj))
                    return self._parse_vision_response(completion, settings.vision_model, provider="custom")
                except (ValueError, TypeError, AttributeError, _MalformedCompletionResponse):
                    raise VisionAnalysisTransportError(
                        "Custom vision provider returned invalid structured output",
                        retryable=False, status_code=422,
                    ) from None
        raise VisionAnalysisTransportError("Custom vision provider unavailable", retryable=True)

    async def analyze_image(self, image_b64: str, media_type: str, filename: str) -> VisionAnalysisResult:
        """Analyze an image through the configured vision route.

        The provider receives only an inline data URI and an instruction. It
        never receives the private artifact path, a private URL, or any
        credential. The OpenRouter route keeps its bounded multi-model chain;
        the gateway route is a single dedicated model and never falls through to
        OpenRouter or OpenAI.
        """
        safe_media_type = _safe_vision_media_type(media_type)
        # Custom transport owns its HTTP client and must not resolve a cloud
        # model chain or construct a gateway client.
        route = None if settings.vision_provider == "custom" else await self._build_vision_route()
        data_uri = f"data:{safe_media_type};base64,{image_b64}"
        instruction = (
            f'Analyze the image "{filename}" for a factual knowledge-base summary. '
            "Return only the requested structured result. Preserve visible text exactly, "
            "including labels and spelling. Describe visible objects, entities, visual "
            "details, and diagram relationships with their visible arrow direction. "
            "If text or a relationship is unreadable or ambiguous, report it in uncertainties "
            "instead of guessing."
        )
        if route is not None and route.provider == "gateway":
            # The gateway enforces only valid JSON, not a schema, so the shape
            # must be stated in the prompt for the typed result to round-trip.
            instruction += _GATEWAY_VISION_FIELD_CONTRACT
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
            ],
        }]
        if settings.vision_provider == "custom":
            return await self._complete_custom_vision(messages)
        assert route is not None
        return await self._complete_vision_with_fallback(messages, route=route)

    async def generate_tags(
        self, text: str, existing_tags: list[str] | None = None, model: str | None = None
    ) -> tuple[list[str], list[str]]:
        """Generate tags and categories from text.

        Returns (tags, categories) where:
        - tags: specific keywords/topics (5-10 items)
        - categories: broad subject areas (2-4 items)
        """
        existing_hint = (
            f" Existing tags for context: {', '.join(existing_tags)}." if existing_tags else ""
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a tagging assistant. Given text, extract specific tags (keywords/topics, 5-10) "
                    "and broad categories (subject areas, 2-4). "
                    "Respond ONLY with valid JSON in this exact format: "
                    '{"tags": ["tag1", "tag2"], "categories": ["cat1", "cat2"]}'
                ),
            },
            {
                "role": "user",
                "content": f"Extract tags and categories from this text.{existing_hint}\n\n{text}",
            },
        ]
        try:
            data = await self.complete_structured(
                messages,
                TagExtraction,
                schema_name="tag_extraction",
                model=model,
            )
            assert isinstance(data, TagExtraction)
            return (
                _normalize_terms(data.tags, limit=10),
                _normalize_terms(data.categories, limit=4),
            )
        except Exception as exc:
            logger.warning("Failed to parse LLM tag response: %s", exc)
            return [], []

    async def extract_entities(
        self, text: str, model: str | None = None
    ) -> ExtractedEntities:
        """Extract named entities from text as structured data.

        Returns an ExtractedEntities instance with people, organizations, dates,
        and key_topics arrays. Returns empty lists on any failure — never raises.
        """
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You extract named entities from text for a knowledge base. "
                        "Respond ONLY with valid JSON in this exact format: "
                        '{"people": ["Alice Smith"], "organizations": ["Anthropic"], '
                        '"dates": ["Q3 2025"], "key_topics": ["transformer architecture"]}. '
                        "Use empty arrays when no entities of a type are found."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Extract entities from this text:\n\n{text}",
                },
            ]
            data = await self.complete_structured(
                messages,
                ExtractedEntities,
                schema_name="extracted_entities",
                model=model,
            )
            assert isinstance(data, ExtractedEntities)
            return data
        except Exception as exc:
            logger.warning("Entity extraction failed: %s", exc)
            return ExtractedEntities()
