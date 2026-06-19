import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_api_key
from app.database import get_db
from app.schemas.conversation import (
    ConversationCreateRequest,
    ConversationOut,
    ConversationWithMessages,
    ConversationMessageOut,
    AppendMessagesRequest,
    UpdateTitleRequest,
)

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE tenant_id = :tenant_id ORDER BY updated_at DESC"
        ),
        {"tenant_id": tenant_id},
    )
    rows = result.mappings().all()
    return [ConversationOut.model_validate(dict(row)) for row in rows]


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(
            "INSERT INTO conversations (title, tenant_id) VALUES (:title, :tenant_id) "
            "RETURNING id, title, created_at, updated_at"
        ),
        {"title": body.title, "tenant_id": tenant_id},
    )
    await db.commit()
    row = result.mappings().one()
    return ConversationOut.model_validate(dict(row))


@router.get("/{conv_id}", response_model=ConversationWithMessages)
async def get_conversation(conv_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    conv_result = await db.execute(
        text(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": conv_id, "tenant_id": tenant_id},
    )
    conv_row = conv_result.mappings().one_or_none()
    if conv_row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs_result = await db.execute(
        text(
            "SELECT id, conversation_id, role, content, created_at "
            "FROM conversation_messages "
            "WHERE conversation_id = :id AND tenant_id = :tenant_id "
            "ORDER BY created_at ASC"
        ),
        {"id": conv_id, "tenant_id": tenant_id},
    )
    msg_rows = msgs_result.mappings().all()
    messages = [ConversationMessageOut.model_validate(dict(r)) for r in msg_rows]

    return ConversationWithMessages.model_validate(
        {
            **dict(conv_row),
            "messages": messages,
        }
    )


@router.delete("/{conv_id}", status_code=204)
async def delete_conversation(conv_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text("DELETE FROM conversations WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": conv_id, "tenant_id": tenant_id},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.post("/{conv_id}/messages", response_model=ConversationWithMessages)
async def append_messages(
    conv_id: uuid.UUID,
    body: AppendMessagesRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    # Verify conversation exists and belongs to this tenant
    conv_result = await db.execute(
        text("SELECT id FROM conversations WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": conv_id, "tenant_id": tenant_id},
    )
    if conv_result.mappings().one_or_none() is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Insert messages
    for msg in body.messages:
        await db.execute(
            text(
                "INSERT INTO conversation_messages (conversation_id, role, content, tenant_id) "
                "VALUES (:conv_id, :role, :content, :tenant_id)"
            ),
            {
                "conv_id": conv_id,
                "role": msg.role,
                "content": msg.content,
                "tenant_id": tenant_id,
            },
        )

    # Update updated_at
    await db.execute(
        text("UPDATE conversations SET updated_at = now() WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": conv_id, "tenant_id": tenant_id},
    )
    await db.commit()

    return await get_conversation(conv_id, request, db)


@router.patch("/{conv_id}", response_model=ConversationOut)
async def update_title(
    conv_id: uuid.UUID,
    body: UpdateTitleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    result = await db.execute(
        text(
            "UPDATE conversations SET title = :title, updated_at = now() "
            "WHERE id = :id AND tenant_id = :tenant_id "
            "RETURNING id, title, created_at, updated_at"
        ),
        {"title": body.title, "id": conv_id, "tenant_id": tenant_id},
    )
    await db.commit()
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationOut.model_validate(dict(row))
