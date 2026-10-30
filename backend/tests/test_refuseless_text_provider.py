"""Refuseless text-provider lane: selection, chain, key handling, isolation.

Every test here runs against fake transports. No provider, database, or cluster
is contacted, and the credentials are synthetic placeholders.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.llm import LLMService

ASYNC_CLIENT_TARGET = "app.services.llm.AsyncOpenAI"

PRIMARY = "deepseek-v4.1-flash-fast"
FALLBACK = "minimax-m3.1-flash"
BASE_URL = "https://api.refuseless.com/v1"
SYNTHETIC_KEY = "synthetic-refuseless-key-not-a-real-credential"
SYNTHETIC_OPENROUTER_KEY = "synthetic-openrouter-key"
SYNTHETIC_OPENAI_KEY = "synthetic-openai-key"
SYNTHETIC_VISION_KEY = "synthetic-vision-key"


class _FakeCompletions:
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


class _FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def _response(content: str = "ok", model: str = PRIMARY):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=None,
        model=model,
        provider="refuseless-upstream",
    )


def _api_status_error(status_code: int):
    from openai import APIStatusError

    request = httpx.Request("POST", BASE_URL + "/chat/completions")
    response = httpx.Response(status_code, request=request)
    return APIStatusError("request failed", response=response, body={})


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://u:p@localhost/test",
        "openai_api_key": SYNTHETIC_OPENAI_KEY,
        "api_key": "test-api-key",
        "llm_api_key": SYNTHETIC_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def refuseless(monkeypatch):
    created: list = []

    def _fake_client(*_args, **kwargs):
        client = _FakeClient()
        client.ctor_kwargs = dict(kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(ASYNC_CLIENT_TARGET, _fake_client)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.services.llm.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("app.services.llm.settings.llm_provider", "refuseless")
    monkeypatch.setattr("app.services.llm.settings.llm_api_key", SYNTHETIC_KEY)
    monkeypatch.setattr("app.services.llm.settings.llm_base_url", BASE_URL)
    monkeypatch.setattr("app.services.llm.settings.llm_default_model", PRIMARY)
    monkeypatch.setattr("app.services.llm.settings.llm_fallback_models", FALLBACK)
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_api_key", SYNTHETIC_OPENROUTER_KEY
    )
    monkeypatch.setattr("app.services.llm.settings.openai_api_key", SYNTHETIC_OPENAI_KEY)
    service = LLMService()
    return service, created


def test_default_settings_are_the_refuseless_provider() -> None:
    settings = _settings()

    assert settings.llm_provider == "refuseless"
    assert settings.llm_base_url == BASE_URL
    assert settings.llm_default_model == PRIMARY
    assert settings.llm_fallback_models == FALLBACK


def test_default_chain_is_deepseek_then_minimax_flash(refuseless) -> None:
    service, _created = refuseless

    assert service.default_model == PRIMARY
    assert service._build_model_chain(None) == [PRIMARY, FALLBACK]


def test_unknown_provider_fails_at_boot() -> None:
    with pytest.raises(ValidationError, match="LLM_PROVIDER"):
        _settings(llm_provider="bedrock")


def test_refuseless_provider_requires_a_key() -> None:
    # Settings can be built without a key so a migration job can import the
    # module for its database URL; the model client is what refuses to start.
    settings = _settings(llm_api_key="")
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        settings.require_llm_credentials()


def test_refuseless_provider_rejects_a_placeholder_key() -> None:
    settings = _settings(llm_api_key="sk-refuseless-...")
    with pytest.raises(ValueError, match="placeholder"):
        settings.require_llm_credentials()


def test_llm_service_refuses_to_start_without_a_key(monkeypatch) -> None:
    settings = _settings(llm_api_key="")
    monkeypatch.setattr("app.services.llm.settings", settings)
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        LLMService()


def test_no_openrouter_client_is_constructed_for_refuseless(refuseless) -> None:
    _service, created = refuseless

    assert len(created) == 1
    kwargs = created[0].ctor_kwargs
    assert kwargs["base_url"] == BASE_URL
    assert kwargs["api_key"] == SYNTHETIC_KEY
    assert "openrouter" not in str(kwargs.get("base_url", ""))
    # No OpenRouter attribution headers travel to the refuseless endpoint.
    assert "default_headers" not in kwargs


@pytest.mark.asyncio
async def test_completion_uses_the_refuseless_chain_and_no_openrouter_extras(
    refuseless,
) -> None:
    service, created = refuseless
    completions = created[0].chat.completions
    completions.outcomes = [_response("hello")]

    result = await service.complete([{"role": "user", "content": "Hi"}])

    assert result == "hello"
    call = completions.calls[0]
    assert call["model"] == PRIMARY
    assert "extra_body" not in call


@pytest.mark.asyncio
async def test_structured_request_skips_the_openrouter_routing_hint(refuseless) -> None:
    from app.services.llm import TagExtraction

    service, created = refuseless
    completions = created[0].chat.completions
    completions.outcomes = [_response('{"tags": ["a"], "categories": ["b"]}')]

    await service.complete_structured(
        [{"role": "user", "content": "tags"}], TagExtraction, schema_name="tags"
    )

    call = completions.calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert "extra_body" not in call


@pytest.mark.asyncio
async def test_falls_back_to_the_second_refuseless_model(refuseless) -> None:
    service, created = refuseless
    completions = created[0].chat.completions
    completions.outcomes = [_api_status_error(401), _response("fallback answer", FALLBACK)]

    result = await service.complete([{"role": "user", "content": "Hi"}])

    assert result == "fallback answer"
    assert [call["model"] for call in completions.calls] == [PRIMARY, FALLBACK]


@pytest.mark.asyncio
async def test_gateway_5xx_is_retried_on_the_same_model_then_succeeds(refuseless) -> None:
    service, created = refuseless
    completions = created[0].chat.completions
    completions.outcomes = [_api_status_error(500), _response("recovered")]

    result = await service.complete([{"role": "user", "content": "Hi"}])

    assert result == "recovered"
    # One bounded same-model retry, then success; no model change.
    assert [call["model"] for call in completions.calls] == [PRIMARY, PRIMARY]


@pytest.mark.asyncio
async def test_gateway_5xx_exhaustion_moves_to_the_configured_fallback(refuseless) -> None:
    service, created = refuseless
    completions = created[0].chat.completions
    completions.outcomes = [
        _api_status_error(502),
        _api_status_error(502),
        _api_status_error(502),
        _response("from fallback", FALLBACK),
    ]

    result = await service.complete([{"role": "user", "content": "Hi"}])

    assert result == "from fallback"
    # Two bounded retries on the primary, then the configured fallback model.
    assert [call["model"] for call in completions.calls] == [PRIMARY, PRIMARY, PRIMARY, FALLBACK]


@pytest.mark.asyncio
async def test_openrouter_keeps_its_original_5xx_behaviour(refuseless, monkeypatch) -> None:
    # Control: the same status that earns a retry on refuseless still fails
    # closed immediately on the OpenRouter route, which this change must not
    # alter.
    service, _created = refuseless
    monkeypatch.setattr(service, "provider", "openrouter")

    assert 500 not in service._transient_statuses()
    assert service._get_fallback_delay(500, 0) is None
    assert 429 in service._transient_statuses()


@pytest.mark.asyncio
async def test_vision_custom_route_uses_its_own_url_model_and_credential(
    monkeypatch,
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"summary": "label", "image_type": "diagram"}'}}
                ],
                "model": "resolved-vision",
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    monkeypatch.setattr("app.services.llm.settings.llm_provider", "refuseless")
    monkeypatch.setattr("app.services.llm.settings.llm_api_key", SYNTHETIC_KEY)
    monkeypatch.setattr(
        "app.services.llm.settings.openrouter_api_key", SYNTHETIC_OPENROUTER_KEY
    )
    monkeypatch.setattr("app.services.llm.settings.openai_api_key", SYNTHETIC_OPENAI_KEY)
    monkeypatch.setattr("app.services.llm.settings.vision_provider", "custom")
    monkeypatch.setattr(
        "app.services.llm.settings.vision_base_url", "https://vision.example.test/v1"
    )
    monkeypatch.setattr("app.services.llm.settings.vision_model", "operator/vision-model")
    monkeypatch.setattr("app.services.llm.settings.vision_api_key", SYNTHETIC_VISION_KEY)
    monkeypatch.setattr("app.services.llm.settings.vision_timeout_seconds", 11.0)

    service = LLMService(vision_transport=httpx.MockTransport(handle))
    result = await service.analyze_image("aW1hZ2U=", "image/png", "diagram.png")

    (request,) = seen
    body = json.loads(request.content)
    assert str(request.url) == "https://vision.example.test/v1/chat/completions"
    assert body["model"] == "operator/vision-model"
    assert request.headers["authorization"] == f"Bearer {SYNTHETIC_VISION_KEY}"
    # The vision route never borrows the text or OpenRouter credential.
    assert SYNTHETIC_OPENROUTER_KEY not in str(request.headers)
    assert SYNTHETIC_OPENAI_KEY not in str(request.headers)
    assert SYNTHETIC_KEY not in str(request.headers)
    assert result.provider.provider == "custom"
    assert result.provider.requested_model == "operator/vision-model"


def test_vision_key_defaults_to_the_single_text_provider_secret() -> None:
    settings = _settings()

    assert settings.vision_provider == "custom"
    assert settings.vision_api_key == SYNTHETIC_KEY


@pytest.mark.asyncio
async def test_credential_is_never_logged(refuseless, caplog) -> None:
    service, created = refuseless
    completions = created[0].chat.completions
    completions.outcomes = [
        _api_status_error(503),
        _api_status_error(503),
        _api_status_error(503),
        _api_status_error(503),
        _response("ok"),
    ]

    with caplog.at_level(logging.DEBUG):
        try:
            await service.complete([{"role": "user", "content": "Hi"}])
        except RuntimeError:
            pass
        assert SYNTHETIC_KEY not in caplog.text
        assert SYNTHETIC_KEY not in repr(service)
        assert SYNTHETIC_KEY not in repr(service.client)
        assert SYNTHETIC_KEY not in repr(_settings())
