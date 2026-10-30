"""Offline HTTP-boundary tests: no provider or database connections."""
import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.services.llm import LLMService, VisionAnalysisTransportError


@pytest.fixture
def custom(monkeypatch):
    monkeypatch.setattr(settings, "vision_provider", "custom")
    monkeypatch.setattr(settings, "vision_base_url", "http://lux.internal:8080/prefix/v1/")
    monkeypatch.setattr(settings, "vision_model", "operator/local-vision")
    monkeypatch.setattr(settings, "vision_api_key", "")
    monkeypatch.setattr(settings, "vision_timeout_seconds", 12.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["", "dedicated-test-key"])
@pytest.mark.parametrize("schema_rejected", [False, True])
async def test_custom_request_and_parse(custom, monkeypatch, auth, schema_rejected):
    monkeypatch.setattr(settings, "vision_api_key", auth)
    requests = []

    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        assert str(request.url) == "http://lux.internal:8080/prefix/v1/chat/completions"
        assert request.headers.get("authorization") == (f"Bearer {auth}" if auth else None)
        assert request.extensions["timeout"]["read"] == 12.0
        assert body["model"] == "operator/local-vision"
        assert "provider" not in body and "extra_body" not in body
        assert body["messages"][0]["content"][1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="
        if schema_rejected and len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unsupported"}})
        assert ("response_format" in body) is (not schema_rejected)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '<think>check</think>```json\n{"summary":"Visible label","image_type":"diagram"}\n```'}}],
            "model": "local-resolved", "usage": {"prompt_tokens": 9, "completion_tokens": 5},
        })

    service = LLMService(vision_transport=httpx.MockTransport(handle))
    async def forbidden(*args, **kwargs):
        pytest.fail("custom vision escaped to cloud/text")
    monkeypatch.setattr(service, "_create_text_completion", forbidden)
    monkeypatch.setattr(service, "complete", forbidden)
    result = await service.analyze_image("aW1hZ2U=", "image/png", "diagram.png")
    assert len(requests) == (2 if schema_rejected else 1)
    assert result.analysis.summary == "Visible label"
    assert result.provider.provider == "custom"
    assert result.provider.requested_model == "operator/local-vision"
    assert result.provider.returned_model == "local-resolved"
    assert result.provider.usage.input_tokens == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["redirect", "timeout", "auth", "server", "invalid", "schema_twice", "plain"])
async def test_custom_failures_never_escape(custom, monkeypatch, failure):
    calls = []
    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private key and image bytes", request=request)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://cloud.invalid/stolen"})
        if failure == "plain":
            return httpx.Response(200, text="private garbage")
        if failure == "invalid":
            return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
        status = {"auth": 401, "server": 503, "schema_twice": 422}[failure]
        return httpx.Response(status, json={"error": {"message": "response_format unsupported private data"}})
    service = LLMService(vision_transport=httpx.MockTransport(handle))
    async def forbidden(*args, **kwargs):
        pytest.fail("custom vision escaped")
    monkeypatch.setattr(service, "_create_text_completion", forbidden)
    monkeypatch.setattr(service, "complete", forbidden)
    with pytest.raises(VisionAnalysisTransportError) as error:
        await service.analyze_image("private-image", "image/png", "private-file")
    assert "private" not in str(error.value)
    assert len(calls) == (2 if failure == "schema_twice" else 1)
    assert error.value.retryable is (failure in {"timeout", "server"})


def configured(**kwargs):
    return Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@localhost/test", redis_url="redis://localhost", openai_api_key="test", openrouter_api_key="test", llm_api_key="test-llm-key", api_key="test", **kwargs)


@pytest.mark.parametrize("url", ["http://u:p@host/v1", "http://host/v1?key=x", "http://host/v1#fragment", "file:///v1", "http://", "http://host/?", "http://host/#"])
def test_bad_custom_urls(url):
    with pytest.raises(ValidationError):
        configured(vision_provider="custom", vision_base_url=url, vision_model="local")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 301])
def test_bad_timeout(timeout):
    with pytest.raises(ValidationError):
        configured(vision_timeout_seconds=timeout)


def test_custom_requires_dedicated_configuration():
    # The route ships refuseless defaults, so the guard is proven by explicitly
    # blanking them: custom vision still refuses to run without a dedicated
    # base URL and model.
    with pytest.raises(ValidationError):
        configured(vision_provider="custom", vision_base_url="", vision_model="")


def test_m27_is_not_vision():
    with pytest.raises(ValidationError):
        configured(openrouter_vision_fallback_models="minimax/minimax-m2.7")
