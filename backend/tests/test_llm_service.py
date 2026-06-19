from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from app.services.llm import BrowserActions, LLMService, TagExtraction, _strict_json_schema


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
    )


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
async def test_classify_relationship_clamps_invalid_confidence(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response('{"relationship": "expands_on", "confidence": 4.2}')
    ]

    result = await service.classify_relationship("A", "summary", "B", "summary")

    assert result == ("expands_on", 1.0)


@pytest.mark.asyncio
async def test_browser_actions_parse_reasoning_wrapped_array(llm_service) -> None:
    service, openrouter_completions, _openai_completions = llm_service
    openrouter_completions.outcomes = [
        _completion_response('<think>cookie wall</think>\n[{"action": "click", "text": "Accept all"}]')
    ]

    actions = await service.get_browser_actions("Cookie settings", "https://example.test")

    assert actions == [{"action": "click", "text": "Accept all"}]
