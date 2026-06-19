"""RAG chat service — retrieves relevant chunks and answers grounded in context."""
import logging
import uuid
from collections.abc import AsyncIterator, Callable

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.schemas.chat import ChatMessage, ChatResponse, ChatSource, UsageInfo
from app.services.artifact_citations import build_artifact_citation
from app.services.embedder import EmbeddingService
from app.services.llm import LLMService
from app.services.search import SearchService

logger = logging.getLogger(__name__)

_RETRIEVAL_LIMIT = 8
_MIN_SCORE = 0.3
_MAX_CONTEXT_CHARS = 24000  # ~6000 tokens at ~4 chars/token
_CHUNK_TEXT_MAX = 500  # characters for source chunk_text in API responses

_SYSTEM_PROMPT = """\
You are a personal knowledge assistant. Answer the user's question based ONLY on the \
following context from their knowledge base. If the context doesn't contain relevant \
information, say so honestly — do not make up facts.

Always cite your sources by referencing the item title and source type.

CONTEXT:
{context}"""

_NO_CONTEXT_REPLY = (
    "I don't have information about that in your knowledge base. "
    "Try ingesting relevant content first."
)


async def persist_messages_background(
    conversation_id: uuid.UUID,
    tenant_id: str,
    user_content: str,
    assistant_content: str,
) -> None:
    """Persist user + assistant messages to conversation_messages.

    Self-contained: creates and closes its own DB session so it is safe to call
    from FastAPI BackgroundTasks (the request-scoped session will already be closed).
    """
    try:
        async with async_session() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO conversation_messages "
                        "(conversation_id, role, content, tenant_id) VALUES "
                        "(:conv_id, 'user', :content, :tenant_id), "
                        "(:conv_id, 'assistant', :assistant_content, :tenant_id)"
                    ),
                    {
                        "conv_id": conversation_id,
                        "content": user_content,
                        "assistant_content": assistant_content,
                        "tenant_id": tenant_id,
                    },
                )
                await session.execute(
                    text(
                        "UPDATE conversations SET updated_at = now() "
                        "WHERE id = :id AND tenant_id = :tenant_id"
                    ),
                    {"id": conversation_id, "tenant_id": tenant_id},
                )
    except Exception as exc:
        logger.warning("Failed to persist conversation messages: %s", exc)


async def persist_streamed_messages_background(
    conversation_id: uuid.UUID,
    tenant_id: str,
    user_content: str,
    assistant_tokens: list[str],
) -> None:
    """Persist streamed chat after the response closes.

    The response background task runs even when the client disconnects early,
    so we join whatever tokens were emitted before the stream stopped.
    """
    await persist_messages_background(
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        user_content=user_content,
        assistant_content="".join(assistant_tokens),
    )


class ChatService:
    def __init__(self, db: AsyncSession, embedder: EmbeddingService, llm: LLMService, tenant_id: str = "default"):
        self.db = db
        self.embedder = embedder
        self.llm = llm
        self.tenant_id = tenant_id

    async def _validate_conversation(self, conversation_id: uuid.UUID) -> None:
        """Raise HTTP 404 if conversation_id does not exist for this tenant."""
        result = await self.db.execute(
            text("SELECT id FROM conversations WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": conversation_id, "tenant_id": self.tenant_id},
        )
        if result.mappings().one_or_none() is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

    async def _tenant_has_ready_items(self) -> bool:
        """Avoid outbound embedding work when the tenant has no searchable corpus yet."""
        result = await self.db.execute(
            text(
                "SELECT 1 FROM items "
                "WHERE tenant_id = :tenant_id AND status = 'ready' AND deleted_at IS NULL "
                "LIMIT 1"
            ),
            {"tenant_id": self.tenant_id},
        )
        return result.first() is not None

    async def _retrieve_and_build(
        self,
        messages: list[ChatMessage],
    ) -> tuple[list[dict], list[ChatSource]]:
        """Shared RAG retrieval + prompt building for both chat() and stream_chat().

        Returns (prompt_messages, sources). If no relevant context found,
        prompt_messages will be empty and sources will be empty — callers should
        short-circuit on empty sources.
        """
        if not await self._tenant_has_ready_items():
            return [], []

        user_message = messages[-1].content
        history = messages[:-1]

        search = SearchService(self.db, self.embedder, self.tenant_id)
        # For follow-up messages, augment the search query with history context
        # so that vague messages like "tell me more" still retrieve relevant chunks.
        search_query = user_message
        if history and len(user_message.split()) < 8:
            prior = " ".join(
                h.content for h in history[-2:] if h.role in ("user", "assistant")
            )
            if prior:
                search_query = f"{prior} {user_message}"
        results = await search.vector_search(search_query, limit=_RETRIEVAL_LIMIT)

        relevant = [r for r in results if r.score >= _MIN_SCORE]

        if not relevant:
            return [], []

        # Build context block, capping at ~6000 tokens (~24000 chars)
        context_parts: list[str] = []
        total_chars = 0
        used_results = []
        for r in relevant:
            snippet = f'[Source: "{r.title}" ({r.source_type})]\n{r.chunk_text}'
            if total_chars + len(snippet) > _MAX_CONTEXT_CHARS:
                break
            context_parts.append(snippet)
            total_chars += len(snippet)
            used_results.append(r)

        context = "\n---\n".join(context_parts)
        system_content = _SYSTEM_PROMPT.format(context=context)

        prompt_messages: list[dict] = [{"role": "system", "content": system_content}]
        for h in history:
            prompt_messages.append({"role": h.role, "content": h.content})
        prompt_messages.append({"role": "user", "content": user_message})

        sources = [
            ChatSource(
                item_id=r.item_id,
                title=r.title,
                source_type=r.source_type,
                # Cap chunk_text in the response; full text stays in the DB
                chunk_text=(r.chunk_text[:_CHUNK_TEXT_MAX] + "…") if len(r.chunk_text) > _CHUNK_TEXT_MAX else r.chunk_text,
                score=r.score,
                chunk_index=r.chunk_index,
                total_chunks=None,  # deferred: add window-function count query in follow-up
                source_url=r.source_url,
                artifact_citation=r.artifact_citation
                or build_artifact_citation(
                    getattr(r, "item_metadata", None),
                    source_url=r.source_url,
                    original_artifact_url=f"/api/v1/items/{r.item_id}/artifact",
                ),
                retrieval_provenance=getattr(r, "retrieval_provenance", None),
            )
            for r in used_results
        ]

        return prompt_messages, sources

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> ChatResponse:
        if conversation_id is not None:
            await self._validate_conversation(conversation_id)

        prompt_messages, sources = await self._retrieve_and_build(messages)

        if not sources:
            if conversation_id is not None:
                await persist_messages_background(
                    conversation_id=conversation_id,
                    tenant_id=self.tenant_id,
                    user_content=messages[-1].content,
                    assistant_content=_NO_CONTEXT_REPLY,
                )
            return ChatResponse(response=_NO_CONTEXT_REPLY, sources=[], usage=None)

        answer, raw_usage = await self.llm.complete_with_usage(prompt_messages, model=model)

        usage = None
        if raw_usage is not None:
            usage = UsageInfo(
                input_tokens=raw_usage["input_tokens"],
                output_tokens=raw_usage["output_tokens"],
            )
        else:
            usage = UsageInfo(input_tokens=None, output_tokens=None)

        if conversation_id is not None:
            await persist_messages_background(
                conversation_id=conversation_id,
                tenant_id=self.tenant_id,
                user_content=messages[-1].content,
                assistant_content=answer,
            )

        return ChatResponse(response=answer, sources=sources, usage=usage)

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> tuple[AsyncIterator[str], Callable[[], dict | None], list[ChatSource]]:
        """Return (token_stream, get_usage).

        Validates conversation_id before returning — raises HTTP 404 immediately if invalid.
        After exhausting the token_stream, call get_usage() for
        {"input_tokens": N, "output_tokens": M} or None.

        Callers are responsible for conversation persistence after the stream exhausts.
        Use persist_messages_background() with FastAPI BackgroundTasks.
        """
        if conversation_id is not None:
            await self._validate_conversation(conversation_id)

        prompt_messages, sources = await self._retrieve_and_build(messages)

        if not sources:
            async def _no_context_stream() -> AsyncIterator[str]:
                yield _NO_CONTEXT_REPLY

            return _no_context_stream(), lambda: None, []

        token_iter, get_usage = await self.llm.stream_complete(prompt_messages, model=model)
        return token_iter, get_usage, sources
