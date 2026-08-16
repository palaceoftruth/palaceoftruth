from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from app.services.llm import (
    BrowserActions,
    LLMCompletionDiagnostics,
    LLMService,
    TagExtraction,
    VisionAnalysisTransportError,
    _strict_json_schema,
)
from app.services.image_analysis import VisionAnalysisResult


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
    monkeypatch.setattr("app.services.llm.settings.openrouter_default_model", "openrouter/primary")
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_fallback_models",
        "openrouter/fallback-a, openrouter/fallback-b",
    )

    service = LLMService()

    return (
        service,
        clients[0].chat.completions,
        clients[1].chat.completions,
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
        "openrouter/primary",
        "openrouter/fallback-a",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_uses_direct_openai_only_after_openrouter_chain_exhausted(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [
        _api_status_error(401),
        _api_status_error(401),
        _api_status_error(401),
    ]
    openai_completions.outcomes = [_completion_response("direct openai answer")]

    result = await service.complete(messages)

    assert result == "direct openai answer"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "openrouter/primary",
        "openrouter/fallback-a",
        "openrouter/fallback-b",
    ]
    assert [call["model"] for call in openai_completions.calls] == ["gpt-4o-mini"]


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
        "openrouter/primary",
        "openrouter/primary",
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
        "openrouter/primary",
        "openrouter/primary",
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
        "openrouter/primary",
        "openrouter/primary",
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
        "openrouter/primary",
        "openrouter/primary",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_uses_direct_openai_after_malformed_openrouter_chain(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    messages = [{"role": "user", "content": "Hello"}]
    openrouter_completions.outcomes = [_malformed_completion_response() for _ in range(9)]
    openai_completions.outcomes = [_completion_response("direct openai answer")]

    result = await service.complete(messages)

    assert result == "direct openai answer"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "openrouter/primary",
        "openrouter/primary",
        "openrouter/primary",
        "openrouter/fallback-a",
        "openrouter/fallback-a",
        "openrouter/fallback-a",
        "openrouter/fallback-b",
        "openrouter/fallback-b",
        "openrouter/fallback-b",
    ]
    assert [call["model"] for call in openai_completions.calls] == ["gpt-4o-mini"]


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
        "openrouter/primary",
        "openrouter/fallback-a",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_complete_with_usage_uses_direct_openai_only_after_openrouter_chain_exhausted(llm_service) -> None:
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

    content, usage = await service.complete_with_usage(messages)

    assert content == "direct openai answer"
    assert usage == {"input_tokens": 17, "output_tokens": 9}
    assert [call["model"] for call in openrouter_completions.calls] == [
        "openrouter/primary",
        "openrouter/fallback-a",
        "openrouter/fallback-b",
    ]
    assert [call["model"] for call in openai_completions.calls] == ["gpt-4o-mini"]


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
    assert openrouter_completions.calls[0]["extra_body"] == {"provider": {"require_parameters": True}}
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
            model="openai/gpt-4.1",
            provider="OpenAI",
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
    assert result.upstream_provider == "OpenAI"
    assert result.requested_model == "openai/gpt-4.1"
    assert result.model == "openai/gpt-4.1"
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
    assert call["extra_body"] == {"provider": {"require_parameters": True}}


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
    assert result.requested_model == "openai/gpt-4.1"
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
        "google/gemini-2.5-flash-lite",
    )
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_vision_fallback_models",
        "openai/gpt-4o-mini",
    )
    openrouter_completions.outcomes = [
        _completion_response(
            '{"summary":"A flow diagram.","image_type":"diagram",'
            '"visible_text":["Start","Done"],"objects":[],"entities":[],'
            '"relationships":[{"source":"Start","target":"Done",'
            '"direction":"source_to_target","label":"next"}],'
            '"visual_details":[],"uncertainties":[]}',
            model="google/gemini-2.5-flash-lite:free",
            prompt_tokens=31,
            completion_tokens=19,
        )
    ]

    result = await service.analyze_image("aW1hZ2U=", "image/png", "diagram.png")

    assert isinstance(result, VisionAnalysisResult)
    assert result.analysis.summary == "A flow diagram."
    assert result.analysis.relationships[0].direction == "source_to_target"
    assert result.provider.requested_model == "google/gemini-2.5-flash-lite"
    assert result.provider.returned_model == "google/gemini-2.5-flash-lite:free"
    assert result.provider.usage is not None
    assert result.provider.usage.input_tokens == 31
    assert result.provider.usage.output_tokens == 19
    call = openrouter_completions.calls[0]
    assert call["model"] == "google/gemini-2.5-flash-lite"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 1024
    assert call["response_format"]["type"] == "json_schema"
    assert call["extra_body"]["provider"] == {"require_parameters": True}
    assert call["extra_body"]["reasoning"] == {"enabled": False}
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
        "google/gemini-2.5-flash-lite"
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 403, 404])
async def test_analyze_image_model_access_errors_use_openrouter_fallback(
    llm_service, status_code: int
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _api_status_error(status_code),
        _completion_response('{"summary":"Fallback summary","image_type":"diagram"}'),
    ]

    result = await service.analyze_image("aW1hZ2U=", "image/png", "diagram.png")

    assert result.provider.requested_model == "openai/gpt-4o-mini"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "google/gemini-2.5-flash-lite",
        "openai/gpt-4o-mini",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_analyze_image_retries_transient_then_uses_openrouter_fallback(llm_service) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        _api_status_error(503),
        _api_status_error(503),
        _api_status_error(503),
        _completion_response('{"summary":"Fallback summary","image_type":"photo"}'),
    ]

    result = await service.analyze_image("aW1hZ2U=", "image/png", "photo.png")

    assert result.analysis.summary == "Fallback summary"
    assert result.provider.requested_model == "openai/gpt-4o-mini"
    assert [call["model"] for call in openrouter_completions.calls] == [
        "google/gemini-2.5-flash-lite",
        "google/gemini-2.5-flash-lite",
        "google/gemini-2.5-flash-lite",
        "openai/gpt-4o-mini",
    ]
    assert openai_completions.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [_api_status_error(429), _api_connection_error()])
async def test_analyze_image_rate_limit_and_network_errors_are_bounded_before_fallback(
    llm_service, failure: Exception
) -> None:
    service, openrouter_completions, openai_completions = llm_service
    openrouter_completions.outcomes = [
        failure,
        failure,
        failure,
        _completion_response('{"summary":"Fallback summary","image_type":"photo"}'),
    ]

    result = await service.analyze_image("aW1hZ2U=", "image/png", "photo.png")

    assert result.provider.requested_model == "openai/gpt-4o-mini"
    assert len(openrouter_completions.calls) == 4
    assert openai_completions.calls == []


@pytest.mark.asyncio
async def test_analyze_image_invalid_output_fails_closed_without_invented_content(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response("not json"),
        _completion_response(""),
    ]

    with pytest.raises(RuntimeError, match="invalid structured output"):
        await service.analyze_image("aW1hZ2U=", "image/png", "photo.png")
