from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, BadRequestError

from app.config import SUPPORTED_TEXT_MODELS, VISION_MODEL
from app.services.llm import (
    BrowserActions,
    LLMCompletionDiagnostics,
    LLMGenerationSettings,
    LLMService,
    TagExtraction,
    VisionAnalysisTransportError,
    _strict_json_schema,
    _vision_gateway_base_url,
)
from app.services.image_analysis import VisionAnalysisResult

FLASH_MODEL = "minimax/minimax-m3.1-flash"


class _FakeCompletionsAPI:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.outcomes: list[object] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("Unexpected completion request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletionsAPI())


def _completion_response(
    content: str,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    model: str = "resolved/test-model",
    provider: str = "test-upstream",
):
    usage = None
    if prompt_tokens is not None and completion_tokens is not None:
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
        model=model,
        provider=provider,
    )


def test_completion_identity_fails_closed_when_provider_omits_actual_model() -> None:
    diagnostics = LLMCompletionDiagnostics()
    response = _completion_response("ok")
    del response.model

    LLMService._record_completion_identity(
        diagnostics,
        response,
        fallback_model="requested/model",
    )

    assert diagnostics.actual_model == "unknown"
    assert diagnostics.upstream_provider == "test-upstream"


def _malformed_completion_response():
    return SimpleNamespace(choices=None, usage=None)


def _provider_error_completion_response(code: int = 504):
    return SimpleNamespace(
        choices=None,
        usage=None,
        error={"code": code, "message": "The operation was aborted"},
    )


def _missing_content_completion_response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
        usage=None,
    )


def _api_status_error(status_code: int) -> APIStatusError:
    request = httpx.Request("POST", "https://example.test/chat/completions")
    response = httpx.Response(status_code, request=request)
    return APIStatusError("request failed", response=response, body={})


def _api_connection_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "https://example.test/chat/completions"))


def _bad_request_error(message: str) -> BadRequestError:
    request = httpx.Request("POST", "https://example.test/chat/completions")
    response = httpx.Response(400, request=request)
    return BadRequestError(message, response=response, body=None)


class _FakeStream:
    """Minimal async-iterable stand-in for a streaming completion response."""

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)

    def __aiter__(self):
        async def _generate():
            for chunk in self._chunks:
                yield chunk

        return _generate()


def _stream_content_chunk(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
        usage=None,
    )


def _stream_usage_chunk(*, prompt_tokens: int, completion_tokens: int):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


_REASONING_DISABLED = {"reasoning": {"enabled": False}}


def test_strict_json_schema_requires_all_object_properties_and_rejects_extras() -> None:
    tag_schema = _strict_json_schema(TagExtraction.model_json_schema())
    assert tag_schema["required"] == ["tags", "categories"]
    assert tag_schema["additionalProperties"] is False

    action_schema = _strict_json_schema(BrowserActions.model_json_schema())
    assert action_schema["required"] == ["actions"]
    assert action_schema["additionalProperties"] is False
    assert action_schema["$defs"]["BrowserAction"]["required"] == ["action", "text"]
    assert action_schema["$defs"]["BrowserAction"]["additionalProperties"] is False


@pytest.fixture
def llm_service(monkeypatch):
    clients: list[_FakeOpenAIClient] = []

    def _fake_async_openai(*_args, **_kwargs):
        client = _FakeOpenAIClient()
        clients.append(client)
        return client

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.services.llm.AsyncOpenAI", _fake_async_openai)
    monkeypatch.setattr("app.services.llm.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr("app.services.llm.settings.openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr("app.services.llm.settings.openai_api_key", "test-openai-key")
    monkeypatch.setattr("app.services.llm.settings.openrouter_default_model", "minimax/minimax-m3")
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_fallback_models",
        "minimax/minimax-m2.7",
    )

    service = LLMService()

    return (
        service,
        clients[0].chat.completions,
        _FakeCompletionsAPI(),  # Unconnected sentinel: service constructs no direct client.
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_complete_retries_next_openrouter_model_on_model_access_failure(
    llm_service,
    status_code: int,
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _api_status_error(status_code),
        _completion_response("openrouter fallback answer"),
    ]

    result = await service.complete(messages)

    assert result == "openrouter fallback answer"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m2.7",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_fails_closed_after_minimax_chain_exhausted(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _api_status_error(401),
        _api_status_error(401),
        _api_status_error(401),
    ]
    openai_completions.outcomes = [_completion_response("direct openai answer")]

    with pytest.raises(RuntimeError, match="exhausted"):
        await service.complete(messages)
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m2.7",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_retries_same_openrouter_model_on_malformed_completion(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _malformed_completion_response(),
        _completion_response("openrouter primary retry answer"),
    ]

    result = await service.complete(messages)

    assert result == "openrouter primary retry answer"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m3",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_retries_same_openrouter_model_on_provider_error_payload(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _provider_error_completion_response(),
        _completion_response("openrouter primary retry answer"),
    ]

    result = await service.complete(messages)

    assert result == "openrouter primary retry answer"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m3",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_retries_same_openrouter_model_on_missing_message_content(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _missing_content_completion_response(),
        _completion_response("openrouter primary retry answer"),
    ]

    result = await service.complete(messages)

    assert result == "openrouter primary retry answer"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m3",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_retries_same_openrouter_model_on_rate_limit(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _api_status_error(429),
        _completion_response("openrouter primary retry answer"),
    ]

    result = await service.complete(messages)

    assert result == "openrouter primary retry answer"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m3",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_fails_closed_after_malformed_minimax_chain(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [_malformed_completion_response() for _ in range(9)]
    openai_completions.outcomes = [_completion_response("direct openai answer")]

    with pytest.raises(RuntimeError, match="exhausted"):
        await service.complete(messages)
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m3",
        "minimax/minimax-m3",
        "minimax/minimax-m2.7",
        "minimax/minimax-m2.7",
        "minimax/minimax-m2.7",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_complete_with_usage_retries_next_openrouter_model_on_model_access_failure(
    llm_service,
    status_code: int,
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _api_status_error(status_code),
        _completion_response(
            "openrouter fallback answer",
            prompt_tokens=11,
            completion_tokens=5,
        ),
    ]

    content, usage = await service.complete_with_usage(messages)

    assert content == "openrouter fallback answer"
    assert usage == {"input_tokens": 11, "output_tokens": 5}
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m2.7",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_with_usage_fails_closed_after_minimax_chain_exhausted(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _api_status_error(401),
        _api_status_error(401),
        _api_status_error(401),
    ]
    openai_completions.outcomes = [
        _completion_response(
            "direct openai answer",
            prompt_tokens=17,
            completion_tokens=9,
        )
    ]

    with pytest.raises(RuntimeError, match="exhausted"):
        await service.complete_with_usage(messages)
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m2.7",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_classify_relationship_returns_none_on_completion_failure(llm_service, monkeypatch) -> None:
    service, _openrouter_completions, _openai_completions = llm_service

    async def fail_complete(*_args, **_kwargs):
        raise RuntimeError("provider returned malformed response")

    monkeypatch.setattr(service, "complete", fail_complete)

    result = await service.classify_relationship(
        "Item A",
        "Summary A",
        "Item B",
        "Summary B",
    )

    assert result == ("none", 0.0)


@pytest.mark.asyncio
async def test_generate_tags_parses_think_wrapped_structured_json(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response(
            '<think>need taxonomy</think>\n{"tags": [" AI ", "ai", "", "Robotics"], "categories": ["Technology", ""]}'
        )
    ]

    tags, categories = await service.generate_tags("Robotics notes")

    assert tags == ["ai", "robotics"]
    assert categories == ["technology"]
    assert openrouter_completions.calls[0]["response_format"]["type"] == "json_schema"
    assert openrouter_completions.calls[0]["extra_body"] == {
        "provider": {"require_parameters": True}, **_REASONING_DISABLED
    }
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_generate_tags_retries_legacy_parse_for_fenced_prose_json(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response("not json"),
        _completion_response(
            "Here is the JSON:\n```json\n{\"tags\": [\"Ops\", \"Incident\"], \"categories\": [\"Reliability\"]}\n```"
        ),
    ]

    tags, categories = await service.generate_tags("Incident notes")

    assert tags == ["ops", "incident"]
    assert categories == ["reliability"]
    assert len(openrouter_completions.calls) == 2


@pytest.mark.asyncio
async def test_classify_relationship_rejects_out_of_range_confidence(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response(
            '{"relationship_exists": true, "relationship": "expands_on", "confidence": 4.2}'
        ),
        _completion_response(
            '{"relationship_exists": true, "relationship": "expands_on", "confidence": 4.2}'
        ),
    ]

    result = await service.classify_relationship("A", "summary", "B", "summary")

    assert result == ("none", 0.0)


@pytest.mark.asyncio
async def test_classify_relationship_detailed_accepts_reasoning_wrapped_json(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response(
            '<think>compare sources</think>\n'
            '{"relationship_exists": true, "relationship": "related_to", "confidence": 0.85}',
            model="minimax/minimax-m3",
            provider="Minimax",
        )
    ]

    result = await service.classify_relationship_detailed(
        "Northwind incident",
        "Errors affected Northwind release.",
        "Northwind rollback",
        "The Northwind release was reverted.",
    )

    assert result.relationship == "related_to"
    assert result.confidence == 0.85
    assert result.validation_outcome == "valid"
    assert result.provider == "openrouter"
    assert result.upstream_provider == "Minimax"
    assert result.requested_model == "minimax/minimax-m3"
    assert result.model == "minimax/minimax-m3"
    assert result.prompt_version == "relationship-classification-v4"
    assert result.temperature == 0.0
    assert result.seed == 1083
    assert result.fallback_used is False
    assert result.retry_count == 0
    call = openrouter_completions.calls[0]
    assert call["max_tokens"] == 256
    assert call["temperature"] == 0.0
    assert call["seed"] == 1083
    assert call["response_format"]["json_schema"]["name"] == "relationship_classification_v3"
    assert call["extra_body"] == {"provider": {"require_parameters": True}, **_REASONING_DISABLED}


@pytest.mark.asyncio
async def test_classify_relationship_detailed_reports_malformed_and_timeout(llm_service, monkeypatch) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response(
            '{"relationship_exists": true, "relationship": "unsupported", "confidence": 0.7}'
        ),
        _completion_response(
            '{"relationship_exists": true, "relationship": "unsupported", "confidence": 0.7}'
        ),
    ]

    malformed = await service.classify_relationship_detailed("A", "summary", "B", "summary")

    async def timed_out(*_args, **_kwargs):
        raise TimeoutError("provider timeout")

    monkeypatch.setattr(service, "complete_structured", timed_out)
    timeout = await service.classify_relationship_detailed("A", "summary", "B", "summary")

    assert malformed.validation_outcome == "malformed"
    assert malformed.relationship == "none"
    assert malformed.retry_count == 1
    assert timeout.validation_outcome == "timeout"
    assert timeout.relationship == "none"


@pytest.mark.asyncio
async def test_classify_relationship_detailed_fails_closed_without_uncalibrated_fallback(
    llm_service,
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [_malformed_completion_response() for _ in range(9)]
    openai_completions.outcomes = [
        _completion_response(
            '{"relationship_exists": true, "relationship": "related_to", "confidence": 0.9}',
            model="gpt-4o-mini-2024-07-18",
            provider="openai",
        )
    ]

    result = await service.classify_relationship_detailed(
        "Northwind incident",
        "Errors affected Northwind release.",
        "Northwind rollback",
        "The Northwind release was reverted.",
    )

    assert result.relationship == "none"
    assert result.confidence == 0.0
    assert result.validation_outcome == "provider_error"
    assert result.provider == "openrouter"
    assert result.requested_model == "minimax/minimax-m3"
    assert result.model == "unknown"
    assert result.upstream_provider == "unknown"
    assert result.retry_provider == "openrouter"
    assert result.fallback_used is False
    assert result.retry_count == 5
    assert len(openrouter_completions.calls) == 6
    assert openai_completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "request_count"), [(401, 2), (403, 2), (429, 6)])
async def test_relationship_classifier_provider_errors_never_use_generic_fallbacks(
    llm_service,
    status_code: int,
    request_count: int,
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _api_status_error(status_code) for _ in range(request_count)
    ]

    result = await service.classify_relationship_detailed(
        "Northwind incident",
        "Errors affected Northwind release.",
        "Northwind rollback",
        "The Northwind release was reverted.",
    )

    assert result.relationship == "none"
    assert result.validation_outcome == "provider_error"
    assert result.fallback_used is False
    assert len(openrouter_completions.calls) == request_count
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_browser_actions_parse_reasoning_wrapped_array(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response('<think>cookie wall</think>\n[{"action": "click", "text": "Accept all"}]')
    ]

    actions = await service.get_browser_actions("Cookie settings", "https://example.test")

    assert actions == [{"action": "click", "text": "Accept all"}]


@pytest.mark.asyncio
async def test_analyze_image_uses_structured_openrouter_vision_request(llm_service, monkeypatch) -> None:
    service, openrouter_completions, openai_completions = llm_service
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_vision_model",
        "minimax/minimax-m3",
    )
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_vision_fallback_models",
        "",
    )
    openrouter_completions.outcomes = [
        _completion_response(
            '{"summary":"A flow diagram.","image_type":"diagram",'
            '"visible_text":["Start","Done"],"objects":[],"entities":[],'
            '"relationships":[{"source":"Start","target":"Done",'
            '"direction":"source_to_target","label":"next"}],'
            '"visual_details":[],"uncertainties":[]}',
            model="minimax/minimax-m3",
            prompt_tokens=31,
            completion_tokens=19,
        )
    ]

    result = await service.analyze_image("aW1hZ2U=", "image/png", "diagram.png")

    assert isinstance(result, VisionAnalysisResult)
    assert result.analysis.summary == "A flow diagram."
    assert result.analysis.relationships[0].direction == "source_to_target"
    assert result.provider.requested_model == "minimax/minimax-m3"
    assert result.provider.returned_model == "minimax/minimax-m3"
    assert result.provider.usage is not None
    assert result.provider.usage.input_tokens == 31
    assert result.provider.usage.output_tokens == 19
    call = openrouter_completions.calls[0]
    assert call["model"] == "minimax/minimax-m3"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 4096
    assert call["response_format"]["type"] == "json_schema"
    assert call["extra_body"]["provider"] == {"require_parameters": True}
    assert call["extra_body"] == {"provider": {"require_parameters": True}, **_REASONING_DISABLED}
    schema = call["response_format"]["json_schema"]["schema"]
    serialized_schema = json.dumps(schema)
    assert "maxLength" not in serialized_schema
    assert "maxItems" not in serialized_schema
    assert schema["additionalProperties"] is False
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_analyze_image_401_is_terminal_without_vision_fallback(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [_api_status_error(401)]

    with pytest.raises(VisionAnalysisTransportError, match="authentication failed") as failure:
        await service.analyze_image("aW1hZ2U=", "image/png", "diagram.png")

    assert failure.value.retryable is False
    assert failure.value.status_code == 401
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3"
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 403, 404])
async def test_analyze_image_model_access_errors_fail_closed(
    llm_service, status_code: int
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _api_status_error(status_code),
        _completion_response('{"summary":"Fallback summary","image_type":"diagram"}'),
    ]

    with pytest.raises(VisionAnalysisTransportError) as failure:
        await service.analyze_image("aW1hZ2U=", "image/png", "diagram.png")
    assert failure.value.retryable is False
    assert failure.value.status_code == status_code
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
    ]
    assert openrouter_completions.calls[0]["extra_body"] == {
        "provider": {"require_parameters": True}, **_REASONING_DISABLED
    }
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_analyze_image_retries_transient_then_fails_closed(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _api_status_error(503),
        _api_status_error(503),
        _api_status_error(503),
        _completion_response('{"summary":"Fallback summary","image_type":"photo"}'),
    ]

    with pytest.raises(VisionAnalysisTransportError) as failure:
        await service.analyze_image("aW1hZ2U=", "image/png", "photo.png")
    assert failure.value.retryable is True
    assert failure.value.status_code == 503
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m3",
        "minimax/minimax-m3",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [_api_status_error(429), _api_connection_error()])
async def test_analyze_image_rate_limit_and_network_errors_are_bounded_and_closed(
    llm_service, failure: Exception
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        failure,
        failure,
        failure,
        _completion_response('{"summary":"Fallback summary","image_type":"photo"}'),
    ]

    with pytest.raises(VisionAnalysisTransportError) as error:
        await service.analyze_image("aW1hZ2U=", "image/png", "photo.png")
    assert error.value.retryable is True
    assert len(openrouter_completions.calls) == 3
    assert {call["model"] for call in openrouter_completions.calls} == {"minimax/minimax-m3"}
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_analyze_image_invalid_output_fails_closed_without_invented_content(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response("not json"),
        _completion_response(""),
        _completion_response("still not json"),
    ]

    with pytest.raises(RuntimeError, match="invalid structured output"):
        await service.analyze_image("aW1hZ2U=", "image/png", "photo.png")


@pytest.mark.asyncio
async def test_analyze_image_never_uses_gpt_fallback(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _api_status_error(404),
        _api_status_error(404),
        _completion_response(
            '{"summary":"Final fallback summary","image_type":"photo"}',
            model="openai/gpt-4.1-mini",
        ),
    ]

    with pytest.raises(VisionAnalysisTransportError) as failure:
        await service.analyze_image("aW1hZ2U=", "image/png", "photo.png")
    assert failure.value.status_code == 404
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
    ]
    assert openai_completions.calls == []


# MiniMax-only generation routing (this slice). These exercise the real
# LLMService and Settings rather than a mocked policy helper.
@pytest.mark.asyncio
async def test_relationship_classification_requests_minimax_m3(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response(
            '{"relationship_exists": true, "relationship": "related_to", "confidence": 0.85}',
            model="minimax/minimax-m3",
            provider="Minimax",
        )
    ]

    result = await service.classify_relationship_detailed("A", "sa", "B", "sb")

    assert result.requested_model == "minimax/minimax-m3"
    assert openrouter_completions.calls[0]["model"] == "minimax/minimax-m3"
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_generic_completion_exhaustion_fails_closed_without_openai(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [_api_status_error(401), _api_status_error(401)]

    with pytest.raises(RuntimeError):
        await service.complete([{"role": "user", "content": "Hello"}])

    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m2.7",
    ]
    assert openai_completions.calls == []


def test_llm_service_does_not_construct_an_openai_generation_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[_FakeOpenAIClient] = []

    def _fake_async_openai(*_args, **_kwargs):
        client = _FakeOpenAIClient()
        clients.append(client)
        return client

    monkeypatch.setattr("app.services.llm.AsyncOpenAI", _fake_async_openai)
    monkeypatch.setattr("app.services.llm.settings.openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr("app.services.llm.settings.openrouter_default_model", "minimax/minimax-m3")
    monkeypatch.setattr("app.services.llm.settings.openrouter_fallback_models", "minimax/minimax-m2.7")

    service = LLMService()

    assert len(clients) == 1
    assert not hasattr(service, "_openai_client")


@pytest.mark.asyncio
async def test_complete_rejects_unsupported_model_override(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service

    with pytest.raises(ValueError, match="MiniMax"):
        await service.complete(
            [{"role": "user", "content": "Hello"}],
            model="openai/gpt-4.1",
        )

    assert openrouter_completions.calls == []


@pytest.mark.asyncio
async def test_stream_complete_rejects_unsupported_model_override(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service

    with pytest.raises(ValueError, match="MiniMax"):
        await service.stream_complete(
            [{"role": "user", "content": "Hello"}],
            model="openai/gpt-4.1",
        )

    assert openrouter_completions.calls == []


# MiniMax M3 reasoning control (this slice). At a bounded 256-token budget M3
# can spend the whole allowance on hidden reasoning and return empty content
# with finish_reason=length. Every cloud generation request must therefore send
# reasoning.enabled=false, including model-fallback and legacy-parser retries.
@pytest.mark.asyncio
async def test_structured_first_request_and_legacy_retry_both_disable_reasoning(
    llm_service,
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Classify"}]
    # Structured schema request is rejected (matches the live 404), the classifier
    # retries the same model with the legacy JSON parser.
    openrouter_completions.outcomes = [
        _api_status_error(404),
        _completion_response(
            '{"tags": ["backups", "restore-testing"], "categories": ["operations"]}',
            model="minimax/minimax-m3",
            provider="Minimax",
        ),
    ]

    result = await service.complete_structured(
        messages,
        TagExtraction,
        schema_name="tag_extraction",
        model="minimax/minimax-m3",
        allow_model_fallbacks=False,
    )

    assert isinstance(result, TagExtraction)
    assert result.tags == ["backups", "restore-testing"]
    assert result.categories == ["operations"]
    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m3",
    ]
    structured_call, legacy_call = openrouter_completions.calls
    assert "response_format" in structured_call
    assert structured_call["extra_body"]["provider"] == {"require_parameters": True}
    assert structured_call["extra_body"]["reasoning"] == {"enabled": False}
    assert "response_format" not in legacy_call
    assert legacy_call["extra_body"] == _REASONING_DISABLED
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_relationship_classifier_schema_and_legacy_retry_disable_reasoning(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _api_status_error(404),
        _completion_response(
            '{"relationship_exists": true, "relationship": "prerequisite_of", "confidence": 0.85}'
        ),
    ]

    result = await service.classify_relationship_detailed("Backup", "Procedure", "Restore", "Testing")

    assert result.relationship == "prerequisite_of"
    assert result.confidence == 0.85
    assert result.validation_outcome == "valid"
    assert result.retry_count == 1
    assert result.fallback_used is False
    assert len(openrouter_completions.calls) == 2
    schema_call, legacy_call = openrouter_completions.calls
    assert schema_call["response_format"]["json_schema"]["name"] == "relationship_classification_v3"
    assert schema_call["extra_body"] == {
        "provider": {"require_parameters": True}, **_REASONING_DISABLED
    }
    assert "response_format" not in legacy_call
    assert legacy_call["extra_body"] == _REASONING_DISABLED
    for call in openrouter_completions.calls:
        assert call["model"] == "minimax/minimax-m3"
        assert call["max_tokens"] == 256
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_flash_classification_uses_json_object_without_seed(llm_service, monkeypatch) -> None:
    service, openrouter_completions, openai_completions = llm_service
    monkeypatch.setattr(
        "app.services.llm.settings.relationship_classification_model",
        FLASH_MODEL,
    )
    openrouter_completions.outcomes = [
        _completion_response(
            '{"relationship_exists": true, "relationship": "expands_on", "confidence": 0.7}',
            model=FLASH_MODEL,
            provider="Minimax",
        ),
    ]

    result = await service.classify_relationship_detailed("Backup", "Procedure", "Restore", "Testing")

    assert result.validation_outcome == "valid"
    assert result.relationship == "expands_on"
    assert result.confidence == 0.7
    assert result.retry_count == 0
    # The seed is not advertised by Flash, so the request omits it and the
    # diagnostics report the effective request rather than a phantom seed.
    assert result.seed is None
    (call,) = openrouter_completions.calls
    assert call["model"] == FLASH_MODEL
    assert call["response_format"] == {"type": "json_object"}
    assert "json_schema" not in call["response_format"]
    assert "seed" not in call
    assert call["max_tokens"] == 256
    assert call["extra_body"] == {"provider": {"require_parameters": True}}
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_flash_classification_legacy_retry_still_enforces_typed_schema(
    llm_service, monkeypatch
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    monkeypatch.setattr(
        "app.services.llm.settings.relationship_classification_model",
        FLASH_MODEL,
    )
    # Simulate a response_format routing failure; this is not a live probe.
    # The legacy JSON retry must still validate.
    openrouter_completions.outcomes = [
        _api_status_error(404),
        _completion_response(
            '```json\n{"relationship_exists": false, "relationship": "none", "confidence": 0.0}\n```',
            model=FLASH_MODEL,
        ),
    ]

    result = await service.classify_relationship_detailed("Backup", "Procedure", "Restore", "Testing")

    assert result.validation_outcome == "empty"
    assert result.relationship == "none"
    assert result.retry_count == 1
    assert len(openrouter_completions.calls) == 2
    structured_call, legacy_call = openrouter_completions.calls
    assert structured_call["response_format"] == {"type": "json_object"}
    assert "response_format" not in legacy_call
    for call in openrouter_completions.calls:
        assert call["model"] == FLASH_MODEL
        assert "seed" not in call
        assert "reasoning" not in call.get("extra_body", {})
    assert openai_completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [FLASH_MODEL, "minimax/minimax-m3", "minimax/minimax-m2.7"])
@pytest.mark.parametrize("path", ["completion", "transient_retry", "stream", "stream_retry", "stream_fallback"])
async def test_model_reasoning_policy_on_every_request_path(llm_service, model, path) -> None:
    service, completions, direct_completions = llm_service
    if path == "completion":
        completions.outcomes = [_completion_response("ok")]
    elif path == "transient_retry":
        completions.outcomes = [_api_status_error(503), _completion_response("ok")]
    elif path == "stream":
        completions.outcomes = [_FakeStream([_stream_content_chunk("ok")])]
    elif path == "stream_retry":
        completions.outcomes = [
            _bad_request_error("Provider does not support stream_options"),
            _FakeStream([_stream_content_chunk("ok")]),
        ]
    else:
        completions.outcomes = [RuntimeError("stream failed"), _completion_response("ok")]

    if path.startswith("stream"):
        stream, _usage = await service.stream_complete([{"role": "user", "content": "Hi"}], model=model)
        assert "".join([chunk async for chunk in stream]) == "ok"
    else:
        assert await service.complete([{"role": "user", "content": "Hi"}], model=model) == "ok"

    assert len(completions.calls) == (2 if path in {"transient_retry", "stream_retry", "stream_fallback"} else 1)
    for call in completions.calls:
        assert call["model"] == model
        if model == FLASH_MODEL:
            assert "reasoning" not in call.get("extra_body", {})
            assert "seed" not in call
        else:
            assert call["extra_body"]["reasoning"] == {"enabled": False}
    assert direct_completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [FLASH_MODEL, "minimax/minimax-m2.7"])
async def test_json_object_models_reject_invalid_typed_output_on_both_attempts(llm_service, model) -> None:
    service, completions, _direct = llm_service
    completions.outcomes = [_completion_response('{"tags": 123}')] * 2

    with pytest.raises(ValueError):
        await service.complete_structured(
            [{"role": "user", "content": "Extract tags as JSON"}],
            TagExtraction,
            schema_name="tag_extraction",
            model=model,
            allow_model_fallbacks=False,
        )

    first, retry = completions.calls
    assert first["response_format"] == {"type": "json_object"}
    assert "response_format" not in retry
    for call in completions.calls:
        assert ("reasoning" in call.get("extra_body", {})) == (model != FLASH_MODEL)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary,fallback", [
    ("minimax/minimax-m3", FLASH_MODEL),
    (FLASH_MODEL, "minimax/minimax-m2.7"),
])
async def test_structured_fallback_recalculates_capabilities(llm_service, monkeypatch, primary, fallback) -> None:
    service, completions, _direct = llm_service
    monkeypatch.setattr(service, "_fallback_models", [fallback])
    completions.outcomes = [_api_status_error(404), _completion_response('{"tags": ["backup"], "categories": []}')]
    diagnostics = LLMCompletionDiagnostics()

    result = await service.complete_structured(
        [{"role": "user", "content": "Extract JSON tags"}], TagExtraction,
        schema_name="tags", model=primary,
        generation_settings=LLMGenerationSettings(seed=1083),
        diagnostics=diagnostics,
    )

    assert result.tags == ["backup"]
    assert diagnostics.seed == (None if fallback == FLASH_MODEL else 1083)
    assert [call["model"] for call in completions.calls] == [primary, fallback]
    for call in completions.calls:
        model = call["model"]
        assert call["response_format"]["type"] == ("json_schema" if model == VISION_MODEL else "json_object")
        assert ("seed" in call) == (model != FLASH_MODEL)
        assert ("reasoning" in call["extra_body"]) == (model != FLASH_MODEL)


@pytest.mark.asyncio
async def test_flash_explicit_schema_request_removes_caller_reasoning_override(llm_service) -> None:
    service, completions, _direct = llm_service
    completions.outcomes = [_completion_response("{}")]

    await service._create_openrouter_completion(
        FLASH_MODEL, [{"role": "user", "content": "Return JSON"}],
        response_format={"type": "json_schema", "json_schema": {"name": "test", "strict": True, "schema": {"type": "object"}}},
        extra_body={"reasoning": {"enabled": False}},
    )

    (call,) = completions.calls
    assert call["response_format"]["type"] == "json_schema"
    assert call["extra_body"] == {"provider": {"require_parameters": True}}


@pytest.mark.asyncio
async def test_m3_classification_keeps_strict_schema_and_seed(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response(
            '{"relationship_exists": true, "relationship": "related_to", "confidence": 0.5}'
        ),
    ]

    result = await service.classify_relationship_detailed("Backup", "Procedure", "Restore", "Testing")

    assert result.validation_outcome == "valid"
    assert result.seed == 1083
    (call,) = openrouter_completions.calls
    assert call["model"] == VISION_MODEL == "minimax/minimax-m3"
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["name"] == "relationship_classification_v3"
    assert call["seed"] == 1083


@pytest.mark.asyncio
async def test_complete_structured_refuses_unsupported_provider_before_request(
    llm_service,
) -> None:
    service, openrouter_completions, openai_completions = llm_service

    with pytest.raises(ValueError, match="unsupported generation model"):
        await service.complete_structured(
            [{"role": "user", "content": "Classify"}],
            TagExtraction,
            schema_name="tag_extraction",
            model="openai/gpt-4.1",
            allow_model_fallbacks=False,
        )

    assert openrouter_completions.calls == []
    assert openai_completions.calls == []


def test_flash_is_text_only_and_cloud_vision_stays_m3(llm_service, monkeypatch) -> None:
    service, _openrouter_completions, _openai_completions = llm_service
    assert FLASH_MODEL in SUPPORTED_TEXT_MODELS

    # Cloud vision must keep its dedicated M3 boundary even when Flash is a
    # permitted text model.
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_vision_model",
        FLASH_MODEL,
    )
    with pytest.raises(ValueError, match="M3 only"):
        service._build_vision_model_chain()


@pytest.mark.asyncio
async def test_cloud_vision_request_disables_reasoning(llm_service, monkeypatch) -> None:
    service, openrouter_completions, openai_completions = llm_service
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_vision_model",
        "minimax/minimax-m3",
    )
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_vision_fallback_models",
        "",
    )
    openrouter_completions.outcomes = [
        _completion_response(
            '{"summary":"A photo.","image_type":"photo"}',
            model="minimax/minimax-m3",
        )
    ]

    await service.analyze_image("aW1hZ2U=", "image/png", "photo.png")

    call = openrouter_completions.calls[0]
    assert call["extra_body"]["provider"] == {"require_parameters": True}
    assert call["extra_body"]["reasoning"] == {"enabled": False}
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_stream_complete_success_disables_reasoning(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _FakeStream(
            [
                _stream_content_chunk("Hello "),
                _stream_content_chunk("world"),
                _stream_usage_chunk(prompt_tokens=7, completion_tokens=2),
            ]
        )
    ]

    stream, get_usage = await service.stream_complete([{"role": "user", "content": "Hi"}])
    chunks = [chunk async for chunk in stream]

    assert "".join(chunks) == "Hello world"
    assert get_usage() == {"input_tokens": 7, "output_tokens": 2}
    assert len(openrouter_completions.calls) == 1
    call = openrouter_completions.calls[0]
    assert call["stream"] is True
    assert call["stream_options"] == {"include_usage": True}
    assert call["extra_body"] == _REASONING_DISABLED
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_stream_options_unsupported_retry_also_disables_reasoning(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _bad_request_error("Provider does not support stream_options"),
        _FakeStream([_stream_content_chunk("fallback chunk")]),
    ]

    stream, get_usage = await service.stream_complete([{"role": "user", "content": "Hi"}])
    chunks = [chunk async for chunk in stream]

    assert "".join(chunks) == "fallback chunk"
    assert get_usage() is None
    assert len(openrouter_completions.calls) == 2
    initial_call, retry_call = openrouter_completions.calls
    assert initial_call["stream_options"] == {"include_usage": True}
    assert "stream_options" not in retry_call
    assert initial_call["extra_body"] == _REASONING_DISABLED
    assert retry_call["extra_body"] == _REASONING_DISABLED
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_stream_failure_falls_back_to_completion_with_reasoning_disabled(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        RuntimeError("stream transport exploded"),
        _completion_response("non-streamed fallback"),
    ]

    stream, get_usage = await service.stream_complete([{"role": "user", "content": "Hi"}])
    chunks = [chunk async for chunk in stream]

    assert chunks == ["non-streamed fallback"]
    assert get_usage() is None
    assert len(openrouter_completions.calls) == 2
    for call in openrouter_completions.calls:
        assert call["extra_body"] == _REASONING_DISABLED
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_stream_exhaustion_fails_closed_without_openai(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        RuntimeError("stream transport exploded"),
        _api_status_error(401),
        _api_status_error(401),
    ]

    stream, _get_usage = await service.stream_complete([{"role": "user", "content": "Hi"}])
    with pytest.raises(RuntimeError, match="exhausted"):
        _ = [chunk async for chunk in stream]

    assert [call["model"] for call in openrouter_completions.calls] == [
        "minimax/minimax-m3",
        "minimax/minimax-m3",
        "minimax/minimax-m2.7",
    ]
    assert openai_completions.calls == []


# --- Gateway vision route (VISION_PROVIDER=gateway, model qwen-vl) -------------


class _ClosingFakeOpenAIClient(_FakeOpenAIClient):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.ctor_kwargs: dict = {}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def gateway_llm_service(monkeypatch):
    created: list[_ClosingFakeOpenAIClient] = []

    def _fake_async_openai(*_args, **_kwargs):
        client = _ClosingFakeOpenAIClient()
        client.ctor_kwargs = dict(_kwargs)
        created.append(client)
        return client

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.services.llm.AsyncOpenAI", _fake_async_openai)
    monkeypatch.setattr("app.services.llm.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr("app.services.llm.settings.openrouter_api_key", "test-openrouter-key")
    monkeypatch.setattr("app.services.llm.settings.openai_api_key", "test-openai-key")
    monkeypatch.setattr("app.services.llm.settings.vision_provider", "gateway")
    monkeypatch.setattr(
        "app.services.llm.settings.llm_gateway_url", "http://gateway.example.invalid:8080"
    )
    monkeypatch.setattr("app.services.llm.settings.llm_gateway_token", "synthetic-gateway-token")
    monkeypatch.setattr("app.services.llm.settings.vision_gateway_model", "qwen-vl")

    service = LLMService()
    # Only OpenRouter exists initially; no direct OpenAI generation client.
    assert len(created) == 1
    return service, created


def _gateway_completions(client):
    return client.chat.completions


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://gateway.example.invalid:8080", "http://gateway.example.invalid:8080/v1"),
        ("http://gateway.example.invalid:8080/", "http://gateway.example.invalid:8080/v1"),
        ("https://gateway.example.invalid/v1", "https://gateway.example.invalid/v1"),
        ("https://gateway.example.invalid/v1/", "https://gateway.example.invalid/v1"),
    ],
)
def test_vision_gateway_base_url_joins_v1_once(raw: str, expected: str) -> None:
    assert _vision_gateway_base_url(raw) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", ["image/png", "image/jpeg"])
async def test_gateway_vision_sends_text_and_image_blocks_without_fallback(
    gateway_llm_service, media_type: str
) -> None:
    service, created = gateway_llm_service
    await service._get_vision_gateway_client()
    gateway_client = created[-1]
    _gateway_completions(gateway_client).outcomes = [
        _completion_response(
            '{"summary":"A legible diagram.","image_type":"diagram",'
            '"visible_text":["HELLO"],"objects":[],"entities":[],'
            '"relationships":[],"visual_details":[],"uncertainties":[]}',
            model="qwen-vl",
        )
    ]

    result = await service.analyze_image("aW1hZ2U=", media_type, "ocr.png")

    assert result.provider.provider == "gateway"
    assert result.provider.requested_model == "qwen-vl"
    assert result.provider.returned_model == "qwen-vl"
    assert result.analysis.visible_text == ["HELLO"]
    # The gateway client carries only the gateway credential and a /v1 base URL.
    assert gateway_client.ctor_kwargs["api_key"] == "synthetic-gateway-token"
    assert gateway_client.ctor_kwargs["base_url"] == "http://gateway.example.invalid:8080/v1"
    assert gateway_client.ctor_kwargs["max_retries"] == 0
    call = _gateway_completions(gateway_client).calls[0]
    assert call["model"] == "qwen-vl"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 4096
    assert "extra_body" not in call
    # The gateway accepts JSON-object mode, not a full JSON schema.
    assert call["response_format"] == {"type": "json_object"}
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "exactly these keys: summary" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == f"data:{media_type};base64,aW1hZ2U="
    # No paid request or direct OpenAI generation client exists.
    assert created[0].chat.completions.calls == []
    assert len(created) == 2
    assert not hasattr(service, "_openai_client")


@pytest.mark.asyncio
async def test_gateway_vision_fails_closed_without_url_or_token(
    gateway_llm_service, monkeypatch
) -> None:
    service, created = gateway_llm_service
    monkeypatch.setattr("app.services.llm.settings.llm_gateway_url", "")
    with pytest.raises(VisionAnalysisTransportError, match="gateway URL") as failure:
        await service.analyze_image("aW1hZ2U=", "image/png", "ocr.png")
    assert failure.value.retryable is False

    monkeypatch.setattr(
        "app.services.llm.settings.llm_gateway_url", "http://gateway.example.invalid:8080"
    )
    monkeypatch.setattr("app.services.llm.settings.llm_gateway_token", "")
    with pytest.raises(VisionAnalysisTransportError, match="credential"):
        await service.analyze_image("aW1hZ2U=", "image/png", "ocr.png")

    # No gateway client was ever constructed, so no request left the host.
    assert len(created) == 1


@pytest.mark.asyncio
async def test_gateway_vision_rejects_unsupported_media_type(gateway_llm_service) -> None:
    service, created = gateway_llm_service
    with pytest.raises(VisionAnalysisTransportError, match="media type"):
        await service.analyze_image("aW1hZ2U=", "text/html", "ocr.png")
    assert len(created) == 1


@pytest.mark.asyncio
async def test_gateway_vision_invalid_output_fails_closed(gateway_llm_service) -> None:
    service, created = gateway_llm_service
    await service._get_vision_gateway_client()
    gateway_client = created[-1]
    _gateway_completions(gateway_client).outcomes = [_completion_response("not json")]

    with pytest.raises(RuntimeError, match="invalid structured output"):
        await service.analyze_image("aW1hZ2U=", "image/png", "ocr.png")

    assert created[0].chat.completions.calls == []
    assert len(created) == 2
    assert len(_gateway_completions(gateway_client).calls) == 1


@pytest.mark.asyncio
async def test_gateway_vision_provider_error_has_no_paid_fallback(gateway_llm_service) -> None:
    service, created = gateway_llm_service
    await service._get_vision_gateway_client()
    gateway_client = created[-1]
    _gateway_completions(gateway_client).outcomes = [_api_status_error(503)] * 3

    with pytest.raises(VisionAnalysisTransportError, match="unavailable") as failure:
        await service.analyze_image("aW1hZ2U=", "image/png", "ocr.png")

    assert failure.value.retryable is True
    assert len(_gateway_completions(gateway_client).calls) == 3
    assert created[0].chat.completions.calls == []
    assert len(created) == 2


@pytest.mark.asyncio
async def test_gateway_vision_401_is_terminal(gateway_llm_service) -> None:
    service, created = gateway_llm_service
    await service._get_vision_gateway_client()
    gateway_client = created[-1]
    _gateway_completions(gateway_client).outcomes = [_api_status_error(401)]

    with pytest.raises(VisionAnalysisTransportError, match="authentication failed") as failure:
        await service.analyze_image("aW1hZ2U=", "image/png", "ocr.png")

    assert failure.value.retryable is False
    assert len(_gateway_completions(gateway_client).calls) == 1


@pytest.mark.asyncio
async def test_gateway_vision_client_closed_on_aclose(gateway_llm_service) -> None:
    service, created = gateway_llm_service
    await service._get_vision_gateway_client()
    gateway_client = created[-1]

    await service.aclose()

    assert gateway_client.closed is True
    assert created[0].closed is True
    assert service._vision_gateway_client is None


@pytest.mark.asyncio
async def test_gateway_vision_parses_fenced_json_response(gateway_llm_service) -> None:
    """The live llama.cpp gateway wraps its JSON in a code fence; the shared
    tolerant parser must strip it and still validate the typed result."""
    service, created = gateway_llm_service
    await service._get_vision_gateway_client()
    gateway_client = created[-1]
    _gateway_completions(gateway_client).outcomes = [
        _completion_response(
            '```json\n{"summary":"Text \'PALACE 42\' in a bold font.","image_type":"OCR fixture",'
            '"visible_text":["PALACE 42"],"objects":[],"entities":[],"relationships":[],'
            '"visual_details":[],"uncertainties":[]}\n```',
            model="qwen2.5-vl-7b",
        )
    ]

    result = await service.analyze_image("aW1hZ2U=", "image/png", "ocr.png")

    assert result.analysis.visible_text == ["PALACE 42"]
    assert result.provider.provider == "gateway"
    assert result.provider.returned_model == "qwen2.5-vl-7b"


@pytest.mark.asyncio
async def test_gateway_vision_does_not_resolve_cloud_or_custom_route(
    gateway_llm_service, monkeypatch
) -> None:
    service, created = gateway_llm_service

    def forbidden_cloud_chain():
        pytest.fail("gateway resolved a cloud vision chain")

    async def forbidden_custom(*args, **kwargs):
        pytest.fail("gateway entered custom vision transport")

    monkeypatch.setattr(service, "_build_vision_model_chain", forbidden_cloud_chain)
    monkeypatch.setattr(service, "_complete_custom_vision", forbidden_custom)
    await service._get_vision_gateway_client()
    _gateway_completions(created[-1]).outcomes = [
        _completion_response('{"summary":"Gateway only","image_type":"diagram"}', model="qwen-vl")
    ]
    result = await service.analyze_image("aW1hZ2U=", "image/png", "ocr.png")
    assert result.provider.provider == "gateway"
    assert created[0].chat.completions.calls == []
    assert len(created) == 2
