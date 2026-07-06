from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.chat import _get_service, chat_stream, router
from app.auth import AuthContext, verify_memory_auth
from app.schemas.artifact_citation import ArtifactCitation
from app.schemas.chat import ChatMessage, ChatRequest
from app.schemas.retrieval_provenance import RetrievalProvenance
from app.services.chat import ChatService, _NO_CONTEXT_REPLY


class NeverCalledService:
    tenant_id = "tenant-a"

    async def chat(self, *_args, **_kwargs):
        raise AssertionError("chat service should not run for request validation failures")

    async def stream_chat(self, *_args, **_kwargs):
        raise AssertionError("stream_chat should not run for request validation failures")


class FakeStreamService:
    tenant_id = "tenant-a"

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> tuple[AsyncIterator[str], object, list]:
        assert [message.content for message in messages] == ["What changed?"]
        assert model is None
        assert conversation_id is not None

        async def iterator() -> AsyncIterator[str]:
            yield "First token"
            yield " second token"

        return iterator(), lambda: {"input_tokens": 7, "output_tokens": 3}, []


class _EmptyItemsResult:
    def first(self):
        return None


class _UnexpectedQueryResult:
    def first(self):
        raise AssertionError("unexpected query path")


class EmptyCorpusDb:
    async def execute(self, statement, params):
        sql = str(statement)
        if "FROM items" in sql and "status = 'ready'" in sql:
            return _EmptyItemsResult()
        return _UnexpectedQueryResult()


class FailIfCalledEmbedder:
    async def embed_single(self, _text: str):
        raise AssertionError("embedder should not run when no ready items exist")


def _client(service: object) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_verify(request: Request):
        request.state.auth_context = AuthContext(tenant_id="tenant-a", auth_mode="api_key", token_hash_reference="key-hash")
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    app.dependency_overrides[verify_memory_auth] = override_verify
    app.dependency_overrides[_get_service] = lambda: service
    return TestClient(app)


def test_chat_request_validation_rejects_empty_messages_and_blank_content() -> None:
    client = _client(NeverCalledService())

    empty_messages = client.post("/api/v1/chat", json={"messages": []})
    assert empty_messages.status_code == 422

    blank_content = client.post(
        "/api/v1/chat/stream",
        json={"messages": [{"role": "user", "content": "   "}], "conversation_id": str(uuid.uuid4())},
    )
    assert blank_content.status_code == 422


@pytest.mark.asyncio
async def test_chat_service_persists_no_context_reply_when_conversation_is_supplied(monkeypatch) -> None:
    conversation_id = uuid.uuid4()
    persisted: list[dict[str, str | uuid.UUID]] = []

    async def fake_persist(
        conversation_id: uuid.UUID,
        tenant_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        persisted.append(
            {
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "user_content": user_content,
                "assistant_content": assistant_content,
            }
        )

    async def fake_validate(self, _conversation_id: uuid.UUID) -> None:
        return None

    async def fake_retrieve(self, _messages: list[ChatMessage]):
        return [], []

    monkeypatch.setattr("app.services.chat.persist_messages_background", fake_persist)
    monkeypatch.setattr(ChatService, "_validate_conversation", fake_validate)
    monkeypatch.setattr(ChatService, "_retrieve_and_build", fake_retrieve)

    service = ChatService(db=object(), embedder=object(), llm=object(), tenant_id="tenant-a")
    response = await service.chat(
        [ChatMessage(role="user", content="What changed?")],
        conversation_id=conversation_id,
    )

    assert response.response == _NO_CONTEXT_REPLY
    assert persisted == [
        {
            "conversation_id": conversation_id,
            "tenant_id": "tenant-a",
            "user_content": "What changed?",
            "assistant_content": _NO_CONTEXT_REPLY,
        }
    ]


@pytest.mark.asyncio
async def test_chat_service_skips_embeddings_when_tenant_has_no_ready_items(monkeypatch) -> None:
    conversation_id = uuid.uuid4()
    persisted: list[dict[str, str | uuid.UUID]] = []

    async def fake_persist(
        conversation_id: uuid.UUID,
        tenant_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        persisted.append(
            {
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "user_content": user_content,
                "assistant_content": assistant_content,
            }
        )

    async def fake_validate(self, _conversation_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr("app.services.chat.persist_messages_background", fake_persist)
    monkeypatch.setattr(ChatService, "_validate_conversation", fake_validate)

    service = ChatService(
        db=EmptyCorpusDb(),
        embedder=FailIfCalledEmbedder(),
        llm=object(),
        tenant_id="tenant-a",
    )
    response = await service.chat(
        [ChatMessage(role="user", content="What changed?")],
        conversation_id=conversation_id,
    )

    assert response.response == _NO_CONTEXT_REPLY
    assert persisted == [
        {
            "conversation_id": conversation_id,
            "tenant_id": "tenant-a",
            "user_content": "What changed?",
            "assistant_content": _NO_CONTEXT_REPLY,
        }
    ]


@pytest.mark.asyncio
async def test_chat_service_preserves_retrieval_provenance_on_sources(monkeypatch) -> None:
    item_id = uuid.uuid4()
    provenance = RetrievalProvenance(
        modality="image_native",
        candidate_source="browser_capture_image",
        support_level="weak",
        source_url="https://x.com/example/status/123",
        original_artifact_url="https://pbs.twimg.com/media/diagram-large.jpg",
        notes=["image-native evidence has no supporting OCR/caption text"],
    )
    citation = ArtifactCitation(
        kind="browser_image_candidate",
        source_url="https://x.com/example/status/123",
        source_label="Parent social post",
        original_artifact_url="https://pbs.twimg.com/media/diagram-large.jpg",
    )

    async def fake_has_ready_items(self) -> bool:
        return True

    async def fake_vector_search(self, query: str, limit: int):
        assert query == "What does the diagram show?"
        assert limit == 8
        return [
            type(
                "Result",
                (),
                {
                    "item_id": item_id,
                    "title": "Architecture diagram",
                    "source_type": "image_candidate",
                    "chunk_text": "Architecture diagram",
                    "score": 0.88,
                    "chunk_index": 0,
                    "source_url": None,
                    "artifact_citation": citation,
                    "retrieval_provenance": provenance,
                },
            )()
        ]

    class FakeLlm:
        async def complete_with_usage(self, prompt_messages, model=None):
            return "It shows the control plane.", None

    monkeypatch.setattr(ChatService, "_tenant_has_ready_items", fake_has_ready_items)
    monkeypatch.setattr("app.services.search.SearchService.vector_search", fake_vector_search)

    service = ChatService(db=object(), embedder=object(), llm=FakeLlm(), tenant_id="tenant-a")
    response = await service.chat([ChatMessage(role="user", content="What does the diagram show?")])

    assert response.sources[0].retrieval_provenance == provenance
    assert response.sources[0].retrieval_provenance.support_level == "weak"
    assert response.sources[0].artifact_citation == citation
    assert response.sources[0].artifact_citation.source_url == "https://x.com/example/status/123"


@pytest.mark.asyncio
async def test_chat_stream_attaches_persistence_before_stream_completion(monkeypatch) -> None:
    persisted: list[dict[str, object]] = []
    conversation_id = uuid.uuid4()

    async def fake_persist_streamed(
        conversation_id: uuid.UUID,
        tenant_id: str,
        user_content: str,
        assistant_tokens: list[str],
    ) -> None:
        persisted.append(
            {
                "conversation_id": conversation_id,
                "tenant_id": tenant_id,
                "user_content": user_content,
                "assistant_tokens": list(assistant_tokens),
            }
        )

    monkeypatch.setattr("app.api.chat.persist_streamed_messages_background", fake_persist_streamed)

    response = await chat_stream(
        body=ChatRequest(
            messages=[{"role": "user", "content": "What changed?"}],
            conversation_id=conversation_id,
        ),
        svc=FakeStreamService(),
    )

    first_event = await response.body_iterator.__anext__()
    assert first_event == "data: First token\n\n"
    await response.body_iterator.aclose()
    assert response.background is not None
    await response.background()

    assert persisted == [
        {
            "conversation_id": conversation_id,
            "tenant_id": "tenant-a",
            "user_content": "What changed?",
            "assistant_tokens": ["First token"],
        }
    ]
