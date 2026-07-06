import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.auth import require_api_capability
from app.database import get_db
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat import ChatService, persist_streamed_messages_background

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(require_api_capability("write"))])


def _get_service(request: Request, db: AsyncSession = Depends(get_db)) -> ChatService:
    return ChatService(db, request.app.state.embedder, request.app.state.llm, request.state.tenant_id)


@router.post("", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    svc: ChatService = Depends(_get_service),
):
    return await svc.chat(
        body.messages,
        model=body.model,
        conversation_id=body.conversation_id,
    )


@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    svc: ChatService = Depends(_get_service),
):
    # Validate conversation and build the stream before starting SSE.
    # stream_chat() raises HTTP 404 here (before StreamingResponse) if conversation is invalid.
    token_iter, get_usage, sources = await svc.stream_chat(
        body.messages,
        model=body.model,
        conversation_id=body.conversation_id,
    )

    user_content = body.messages[-1].content
    accumulated: list[str] = []
    background = None
    if body.conversation_id is not None:
        # Attach persistence before the stream starts so disconnects do not skip it.
        background = BackgroundTask(
            persist_streamed_messages_background,
            conversation_id=body.conversation_id,
            tenant_id=svc.tenant_id,
            user_content=user_content,
            assistant_tokens=accumulated,
        )

    async def generate():
        async for token in token_iter:
            accumulated.append(token)
            # Escape newlines so each SSE data line stays on one line
            escaped = token.replace("\n", "\\n")
            yield f"data: {escaped}\n\n"

        usage = get_usage()
        if usage is not None:
            usage_event = json.dumps({
                "type": "usage",
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            })
        else:
            usage_event = json.dumps({
                "type": "usage",
                "input_tokens": None,
                "output_tokens": None,
            })
        yield f"data: {usage_event}\n\n"
        sources_event = json.dumps({
            "type": "sources",
            "sources": [source.model_dump(mode="json") for source in sources],
        })
        yield f"data: {sources_event}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        background=background,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
