import asyncio
import copy
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

from openai import AsyncOpenAI, RateLimitError, APIStatusError, BadRequestError
from pydantic import BaseModel, Field

from app.config import settings

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4
_BASE_BACKOFF = 2.0

_OPENAI_FALLBACK_MODEL = "gpt-4o-mini"
_OPENROUTER_IMMEDIATE_FALLBACK_STATUSES = {401, 403, 404}
_OPENROUTER_BACKOFF_FALLBACK_STATUSES = {429, 503}
_OPENROUTER_TRANSIENT_RETRIES = 2

# Bound concurrent outbound LLM calls to avoid 429s under parallel ingest gather
_LLM_SEMAPHORE = asyncio.Semaphore(4)


class _MalformedCompletionResponse(RuntimeError):
    pass


class ExtractedEntities(BaseModel):
    people: list[str] = []
    organizations: list[str] = []
    dates: list[str] = []
    key_topics: list[str] = []


class TagExtraction(BaseModel):
    tags: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)


class RelationshipClassification(BaseModel):
    relationship: str = "none"
    confidence: float = 0.0


class BrowserAction(BaseModel):
    action: str
    text: str


class BrowserActions(BaseModel):
    actions: list[BrowserAction] = Field(default_factory=list)


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


class LLMService:
    """LLM operations via OpenRouter with OpenAI gpt-4o-mini as final fallback."""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "https://github.com/palaceoftruth/palaceoftruth",
                "X-Title": "Palace of Truth",
            },
        )
        # Direct OpenAI client — used only when all OpenRouter free models are exhausted
        self._openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.default_model = settings.openrouter_default_model
        self._fallback_models = [
            m.strip()
            for m in settings.openrouter_fallback_models.split(",")
            if m.strip()
        ]

    def _build_model_chain(self, model: str | None) -> list[str]:
        return [model or self.default_model] + self._fallback_models

    @staticmethod
    def _get_openrouter_fallback_delay(status: int, attempt: int) -> float | None:
        # 401/403/404 usually mean the current model is not usable for this key,
        # so move on immediately instead of jumping straight to direct OpenAI.
        if status in _OPENROUTER_IMMEDIATE_FALLBACK_STATUSES:
            return 0.0
        if status in _OPENROUTER_BACKOFF_FALLBACK_STATUSES:
            return min(_BASE_BACKOFF * (2 ** attempt), 16.0)
        return None

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

    async def _create_validated_openai_completion(
        self,
        messages: list[dict],
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        response = await self._create_openai_completion(messages, response_format=response_format)
        try:
            self._completion_content(response)
        except _MalformedCompletionResponse as exc:
            raise RuntimeError("Direct OpenAI fallback returned a malformed completion") from exc
        return response

    async def _create_openrouter_completion(
        self,
        current_model: str,
        messages: list[dict],
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": current_model,
            "messages": messages,
            "max_tokens": 1024,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
            kwargs["extra_body"] = {"provider": {"require_parameters": True}}
        async with _LLM_SEMAPHORE:
            return await self.client.chat.completions.create(**kwargs)

    async def _create_openai_completion(
        self,
        messages: list[dict],
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": _OPENAI_FALLBACK_MODEL,
            "messages": messages,
            "max_tokens": 1024,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        async with _LLM_SEMAPHORE:
            return await self._openai_client.chat.completions.create(**kwargs)

    async def _complete_with_fallback(
        self,
        messages: list[dict],
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        chain = self._build_model_chain(model)
        for attempt, current_model in enumerate(chain):
            transient_retry = 0
            while True:
                try:
                    response = await self._create_openrouter_completion(
                        current_model,
                        messages,
                        response_format=response_format,
                    )
                    self._completion_content(response)
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
                        break
                    logger.warning(
                        "OpenRouter chain returned malformed completion from %s after %d retries, falling back to %s",
                        current_model,
                        transient_retry,
                        _OPENAI_FALLBACK_MODEL,
                    )
                    return await self._create_validated_openai_completion(
                        messages,
                        response_format=response_format,
                    )
                except (RateLimitError, APIStatusError) as e:
                    status = getattr(e, "status_code", 429)
                    if (
                        status in _OPENROUTER_BACKOFF_FALLBACK_STATUSES
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
                        continue
                    delay = self._get_openrouter_fallback_delay(status, attempt)
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
                        break
                    logger.warning(
                        "OpenRouter chain exhausted (%s %d), falling back to %s",
                        current_model,
                        status,
                        _OPENAI_FALLBACK_MODEL,
                    )
                    return await self._create_validated_openai_completion(
                        messages,
                        response_format=response_format,
                    )
        raise RuntimeError("LLM completion failed after all retries and fallbacks")

    async def complete(self, messages: list[dict], model: str | None = None) -> str:
        """Send chat completion with automatic fallback through the model chain.

        Order: OpenRouter primary → OpenRouter fallbacks → OpenAI gpt-4o-mini (direct).
        """
        response = await self._complete_with_fallback(messages, model=model)
        return self._completion_content(response)

    async def complete_structured(
        self,
        messages: list[dict],
        schema: type[BaseModel],
        *,
        schema_name: str,
        model: str | None = None,
    ) -> BaseModel:
        """Request schema-shaped JSON, with one legacy-parser retry for older providers."""
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": _strict_json_schema(schema.model_json_schema()),
            },
        }
        try:
            response = await self._complete_with_fallback(
                messages,
                model=model,
                response_format=response_format,
            )
            return schema.model_validate(_json_loads_from_response(self._completion_content(response)))
        except Exception as exc:
            logger.warning("Structured LLM response failed for %s; retrying legacy JSON parse: %s", schema_name, exc)
            raw = await self.complete(messages, model=model)
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
        Falls back to complete() (full retry + gpt-4o-mini) on streaming errors.

        Note: some OpenRouter upstreams (e.g. xAI/Grok) reject stream_options — on
        BadRequestError containing "stream_options", the stream is retried without usage
        tracking and get_usage() returns None.
        """
        usage_store: dict[str, dict | None] = {"value": None}

        async def _stream_inner() -> AsyncIterator[str]:
            try:
                async with _LLM_SEMAPHORE:
                    stream = await self.client.chat.completions.create(
                        model=model or self.default_model,
                        messages=messages,
                        max_tokens=1024,
                        stream=True,
                        stream_options={"include_usage": True},
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
                                model=model or self.default_model,
                                messages=messages,
                                max_tokens=1024,
                                stream=True,
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
            except Exception as exc:
                logger.warning("Streaming failed (%s), falling back to complete(): %s", type(exc).__name__, exc)
                try:
                    response = await self.complete(messages, model=model)
                    if response:
                        yield response
                except Exception as fallback_exc:
                    logger.error("Fallback complete() also failed: %s", fallback_exc)

        def get_usage() -> dict | None:
            return usage_store["value"]

        return _stream_inner(), get_usage

    async def summarize(self, text: str, model: str | None = None) -> str:
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
        return await self.complete(messages, model=model)

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
        messages = [
            {
                "role": "system",
                "content": (
                    "You classify relationships between knowledge base items. "
                    "Respond ONLY with valid JSON: "
                    '{"relationship": "related_to", "confidence": 0.85}\n'
                    "Valid relationship types: related_to, expands_on, contradicts, "
                    "prerequisite_of, example_of, none"
                ),
            },
            {
                "role": "user",
                "content": (
                    f'ITEM A: "{title_a}"\nSummary: {summary_a}\n\n'
                    f'ITEM B: "{title_b}"\nSummary: {summary_b}\n\n'
                    "What is the relationship of Item A to Item B?"
                ),
            },
        ]
        try:
            data = await self.complete_structured(
                messages,
                RelationshipClassification,
                schema_name="relationship_classification",
            )
            assert isinstance(data, RelationshipClassification)
            rel = str(data.relationship)
            conf = float(data.confidence)
            valid = {"related_to", "expands_on", "contradicts", "prerequisite_of", "example_of", "none"}
            if rel not in valid:
                rel = "none"
            return rel, max(0.0, min(conf, 1.0))
        except Exception as exc:
            logger.warning("Failed to classify relationship: %s", exc)
            return "none", 0.0

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

    async def analyze_image(self, image_b64: str, media_type: str, filename: str) -> str:
        """Call OpenAI vision API to produce a textual description of an image.

        Uses the direct OpenAI client (not OpenRouter, which doesn't support vision).
        """
        data_uri = f"data:{media_type};base64,{image_b64}"
        response = await self._openai_client.chat.completions.create(
            model=settings.vision_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                    {
                        "type": "text",
                        "text": (
                            f'Analyze the image "{filename}" for a personal knowledge base. '
                            "List all visible objects, transcribe any text verbatim, "
                            "describe the context and setting, note colors and visual characteristics."
                        ),
                    },
                ],
            }],
            max_tokens=1024,
        )
        return response.choices[0].message.content or ""

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
