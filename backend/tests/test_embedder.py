from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import tiktoken

from app.embedding_profile import (
    DEFAULT_EMBEDDING_PROFILE_NAME,
    DEFAULT_EMBEDDING_PROVIDER,
    EMBEDDING_DIMENSIONS,
)
from app.services.embedder import EmbeddingRequestError, EmbeddingService


class _FakeEmbeddingsClient:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=0, embedding=self.vector),
            ]
        )


class _FakeOpenAIClient:
    def __init__(self, vector: list[float]) -> None:
        self.embeddings = _FakeEmbeddingsClient(vector)


def _configure_local_http(monkeypatch, *, profile_name: str = "local-http-gte-modernbert-base-1536") -> None:
    monkeypatch.setattr("app.services.embedder.settings.embedding_provider", "local-http")
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "Alibaba-NLP/gte-modernbert-base")
    monkeypatch.setattr("app.services.embedder.settings.embedding_dimensions", EMBEDDING_DIMENSIONS)
    monkeypatch.setattr("app.services.embedder.settings.embedding_profile_name", profile_name)
    monkeypatch.setattr("app.services.embedder.settings.embedding_experimental_profiles_enabled", False)
    monkeypatch.setattr("app.services.embedder.settings.embedding_local_http_url", "http://embedding.test")
    monkeypatch.setattr("app.services.embedder.settings.embedding_local_http_path", "/embed")
    monkeypatch.setattr("app.services.embedder.settings.embedding_local_http_api_key", "")
    monkeypatch.setattr("app.services.embedder.settings.embedding_local_http_timeout_seconds", 1.0)
    monkeypatch.setattr("app.services.embedder.settings.embedding_local_http_normalize", True)
    monkeypatch.setattr("app.services.embedder.settings.embedding_local_http_truncate", True)


@pytest.mark.asyncio
async def test_embedder_requests_1536_dimensions_for_text_embedding_3_models(monkeypatch) -> None:
    monkeypatch.setattr("app.services.embedder.AsyncOpenAI", lambda api_key: _FakeOpenAIClient([0.1] * EMBEDDING_DIMENSIONS))
    monkeypatch.setattr("app.services.embedder.settings.openai_api_key", "test-key")
    monkeypatch.setattr("app.services.embedder.settings.embedding_provider", DEFAULT_EMBEDDING_PROVIDER)
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "text-embedding-3-large")
    monkeypatch.setattr("app.services.embedder.settings.embedding_dimensions", EMBEDDING_DIMENSIONS)
    monkeypatch.setattr("app.services.embedder.settings.embedding_profile_name", DEFAULT_EMBEDDING_PROFILE_NAME)

    service = EmbeddingService()
    result = await service.embed_texts(["hello"])

    assert len(result) == 1
    assert len(result[0]) == EMBEDDING_DIMENSIONS
    assert service.client.embeddings.calls == [
        {
            "model": "text-embedding-3-large",
            "input": ["hello"],
            "dimensions": EMBEDDING_DIMENSIONS,
        }
    ]


@pytest.mark.asyncio
async def test_embedder_rejects_dimension_mismatch(monkeypatch) -> None:
    monkeypatch.setattr("app.services.embedder.AsyncOpenAI", lambda api_key: _FakeOpenAIClient([0.1] * 3072))
    monkeypatch.setattr("app.services.embedder.settings.openai_api_key", "test-key")
    monkeypatch.setattr("app.services.embedder.settings.embedding_provider", DEFAULT_EMBEDDING_PROVIDER)
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "custom-embedding-model")
    monkeypatch.setattr("app.services.embedder.settings.embedding_dimensions", EMBEDDING_DIMENSIONS)
    monkeypatch.setattr("app.services.embedder.settings.embedding_profile_name", DEFAULT_EMBEDDING_PROFILE_NAME)

    service = EmbeddingService()

    with pytest.raises(RuntimeError, match="Embedding dimension mismatch"):
        await service.embed_texts(["hello"])


@pytest.mark.asyncio
async def test_embedder_returns_empty_batch_without_local_http_request(monkeypatch) -> None:
    _configure_local_http(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = EmbeddingService(http_client=http_client)
        assert await service.embed_texts([]) == []

    assert requests == []


@pytest.mark.asyncio
async def test_embedder_posts_tei_compatible_local_http_batch(monkeypatch) -> None:
    _configure_local_http(monkeypatch)
    monkeypatch.setattr("app.services.embedder.settings.embedding_local_http_api_key", "local-key")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                [0.1] * EMBEDDING_DIMENSIONS,
                [0.2] * EMBEDDING_DIMENSIONS,
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = EmbeddingService(http_client=http_client)
        result = await service.embed_texts(["alpha", "beta"])

    assert [len(vector) for vector in result] == [EMBEDDING_DIMENSIONS, EMBEDDING_DIMENSIONS]
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://embedding.test/embed"
    assert request.headers["authorization"] == "Bearer local-key"
    assert request.read()
    assert request.content
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "inputs": ["alpha", "beta"],
        "normalize": True,
        "truncate": True,
    }


@pytest.mark.asyncio
async def test_embedder_applies_local_profile_query_instruction(monkeypatch) -> None:
    _configure_local_http(monkeypatch, profile_name="local-http-bge-small-en-v1.5")
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[[0.1] * 384])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = EmbeddingService(http_client=http_client)
        await service.embed_single("what is palace?")

    assert json.loads(requests[0].content)["inputs"] == ["query: what is palace?"]


@pytest.mark.asyncio
async def test_embedder_constructs_opt_in_native_profile(monkeypatch) -> None:
    _configure_local_http(monkeypatch, profile_name="local-http-clip-native-image-768")
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "")
    monkeypatch.setattr("app.services.embedder.settings.embedding_experimental_profiles_enabled", True)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[[0.1] * 768])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = EmbeddingService(http_client=http_client)
        result = await service.embed_image_references(["image-placeholder"])

    assert service.profile.profile_kind == "native_image"
    assert service.profile.input_modality == "image"
    assert service.profile.enabled_by_default is False
    assert [len(vector) for vector in result] == [768]
    assert json.loads(requests[0].content)["inputs"] == ["image-placeholder"]


@pytest.mark.asyncio
async def test_embedder_allows_report_only_native_profile_dimension_mismatch(monkeypatch) -> None:
    _configure_local_http(monkeypatch, profile_name="local-http-clip-native-image-768")
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "")
    monkeypatch.setattr("app.services.embedder.settings.embedding_experimental_profiles_enabled", True)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[[0.1] * 384]))
    ) as http_client:
        service = EmbeddingService(http_client=http_client)
        result = await service.embed_image_references(["image-placeholder"])

    assert [len(vector) for vector in result] == [384]


@pytest.mark.asyncio
async def test_embedder_rejects_text_ingestion_with_native_image_profile(monkeypatch) -> None:
    _configure_local_http(monkeypatch, profile_name="local-http-clip-native-image-768")
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "")
    monkeypatch.setattr("app.services.embedder.settings.embedding_experimental_profiles_enabled", True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500))) as http_client:
        service = EmbeddingService(http_client=http_client)
        with pytest.raises(RuntimeError, match="expects image inputs"):
            await service.embed_texts(["image-placeholder"])


@pytest.mark.asyncio
async def test_embedder_rejects_image_inputs_with_text_profile(monkeypatch) -> None:
    _configure_local_http(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500))) as http_client:
        service = EmbeddingService(http_client=http_client)
        with pytest.raises(RuntimeError, match="not native image inputs"):
            await service.embed_image_references(["image-placeholder"])


@pytest.mark.asyncio
async def test_embedder_rejects_local_http_dimension_mismatch(monkeypatch) -> None:
    _configure_local_http(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[[0.1] * 768]))) as http_client:
        service = EmbeddingService(http_client=http_client)
        with pytest.raises(RuntimeError, match="Embedding dimension mismatch"):
            await service.embed_texts(["hello"])


@pytest.mark.asyncio
async def test_embedder_rejects_malformed_local_http_response(monkeypatch) -> None:
    _configure_local_http(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"unexpected": []}))) as http_client:
        service = EmbeddingService(http_client=http_client)
        with pytest.raises(RuntimeError, match="response must be a list"):
            await service.embed_texts(["hello"])


@pytest.mark.asyncio
async def test_embedder_maps_non_retryable_local_http_error(monkeypatch) -> None:
    _configure_local_http(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(400, text="bad model"))) as http_client:
        service = EmbeddingService(http_client=http_client)
        with pytest.raises(RuntimeError, match="returned 400"):
            await service.embed_texts(["hello"])


@pytest.mark.asyncio
async def test_embedder_treats_http_422_validation_as_terminal(monkeypatch) -> None:
    _configure_local_http(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(422, text="input is not processable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = EmbeddingService(http_client=http_client)
        with pytest.raises(EmbeddingRequestError, match="returned 422") as captured:
            await service.embed_texts(["hello"])

    assert captured.value.retryable is False
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_embedder_rejects_duplicate_provider_indices(monkeypatch) -> None:
    class DuplicateIndexClient:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=0, embedding=[0.1]),
                    SimpleNamespace(index=0, embedding=[0.2]),
                ]
            )

    monkeypatch.setattr(
        "app.services.embedder.AsyncOpenAI",
        lambda api_key: SimpleNamespace(embeddings=DuplicateIndexClient()),
    )
    monkeypatch.setattr("app.services.embedder.settings.openai_api_key", "test-key")
    monkeypatch.setattr("app.services.embedder.settings.embedding_provider", DEFAULT_EMBEDDING_PROVIDER)
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "text-embedding-3-small")
    monkeypatch.setattr("app.services.embedder.settings.embedding_dimensions", EMBEDDING_DIMENSIONS)
    monkeypatch.setattr("app.services.embedder.settings.embedding_profile_name", DEFAULT_EMBEDDING_PROFILE_NAME)

    with pytest.raises(EmbeddingRequestError, match="invalid indices") as captured:
        await EmbeddingService().embed_texts(["first", "second"])

    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_embedder_retries_local_http_timeout_then_fails(monkeypatch) -> None:
    _configure_local_http(monkeypatch)

    async def _no_sleep(_wait: float) -> None:
        return None

    monkeypatch.setattr("app.services.embedder.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    requests: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = EmbeddingService(http_client=http_client)
        with pytest.raises(EmbeddingRequestError, match="timed out") as captured:
            await service.embed_texts(["hello"])

    assert captured.value.retryable is True
    assert len(requests) == 5


@pytest.mark.asyncio
async def test_embedder_splits_2048_five_hundred_token_inputs_within_aggregate_budget(monkeypatch) -> None:
    calls: list[dict] = []

    class BatchClient:
        async def create(self, **kwargs):
            calls.append(kwargs)
            data = [
                SimpleNamespace(index=index, embedding=[float(text.split(":", 1)[0])])
                for index, text in enumerate(kwargs["input"])
            ]
            return SimpleNamespace(data=list(reversed(data)))

    monkeypatch.setattr("app.services.embedder.AsyncOpenAI", lambda api_key: SimpleNamespace(embeddings=BatchClient()))
    monkeypatch.setattr("app.services.embedder.settings.openai_api_key", "test-key")
    monkeypatch.setattr("app.services.embedder.settings.embedding_provider", DEFAULT_EMBEDDING_PROVIDER)
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "text-embedding-3-small")
    monkeypatch.setattr("app.services.embedder.settings.embedding_dimensions", EMBEDDING_DIMENSIONS)
    monkeypatch.setattr("app.services.embedder.settings.embedding_profile_name", DEFAULT_EMBEDDING_PROFILE_NAME)
    service = EmbeddingService()
    monkeypatch.setattr(service, "_validate_dimensions", lambda _vectors: None)
    texts = [f"{index}:" + " token" * 500 for index in range(2048)]

    vectors = await service.embed_texts(texts)

    encoding = tiktoken.encoding_for_model("text-embedding-3-small")
    assert len(calls) > 1
    assert sum(len(call["input"]) for call in calls) == 2048
    assert all(len(call["input"]) <= 2048 for call in calls)
    assert all(sum(len(encoding.encode(text)) for text in call["input"]) <= 285_000 for call in calls)
    assert [int(vector[0]) for vector in vectors] == list(range(2048))


@pytest.mark.asyncio
async def test_embedder_splits_on_utf8_byte_budget_and_preserves_order(monkeypatch) -> None:
    _configure_local_http(monkeypatch)
    monkeypatch.setattr("app.services.embedder._MAX_BATCH_BYTES", 10)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        inputs = json.loads(request.content)["inputs"]
        return httpx.Response(200, json=[[float(len(requests)), float(index)] for index, _ in enumerate(inputs)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = EmbeddingService(http_client=http_client)
        monkeypatch.setattr(service, "_validate_dimensions", lambda _vectors: None)
        vectors = await service.embed_texts(["éé", "éé", "éé"])

    assert [json.loads(request.content)["inputs"] for request in requests] == [["éé", "éé"], ["éé"]]
    assert vectors == [[1.0, 0.0], [1.0, 1.0], [2.0, 0.0]]


@pytest.mark.asyncio
async def test_embedder_rejects_oversized_input_before_provider_call(monkeypatch) -> None:
    monkeypatch.setattr("app.services.embedder.AsyncOpenAI", lambda api_key: _FakeOpenAIClient([0.1]))
    monkeypatch.setattr("app.services.embedder.settings.openai_api_key", "test-key")
    monkeypatch.setattr("app.services.embedder.settings.embedding_provider", DEFAULT_EMBEDDING_PROVIDER)
    monkeypatch.setattr("app.services.embedder.settings.embedding_model", "text-embedding-3-small")
    monkeypatch.setattr("app.services.embedder.settings.embedding_dimensions", EMBEDDING_DIMENSIONS)
    monkeypatch.setattr("app.services.embedder.settings.embedding_profile_name", DEFAULT_EMBEDDING_PROFILE_NAME)
    monkeypatch.setattr("app.services.embedder._MAX_INPUT_TOKENS", 2)
    service = EmbeddingService()

    with pytest.raises(EmbeddingRequestError, match="maximum is 2") as captured:
        await service.embed_texts(["one two three"])

    assert captured.value.retryable is False
    assert service.client.embeddings.calls == []


@pytest.mark.asyncio
async def test_embedder_discards_partial_batches_after_terminal_validation_failure(monkeypatch) -> None:
    _configure_local_http(monkeypatch)
    monkeypatch.setattr("app.services.embedder._MAX_BATCH_BYTES", 8)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(400, text="maximum request size")
        inputs = json.loads(request.content)["inputs"]
        return httpx.Response(200, json=[[0.1] for _ in inputs])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        service = EmbeddingService(http_client=http_client)
        monkeypatch.setattr(service, "_validate_dimensions", lambda _vectors: None)
        with pytest.raises(EmbeddingRequestError, match="returned 400") as captured:
            await service.embed_texts(["aaaa", "bbbb", "cccc"])

    assert captured.value.retryable is False
    assert len(requests) == 2
