import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator


class ConversationMessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ConversationWithMessages(ConversationOut):
    messages: list[ConversationMessageOut]


class ConversationCreateRequest(BaseModel):
    title: str = "New conversation"

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value


class ConversationMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content must not be blank")
        return value


class AppendMessagesRequest(BaseModel):
    messages: list[ConversationMessageIn]

    @field_validator("messages")
    @classmethod
    def messages_must_not_be_empty(cls, value: list[ConversationMessageIn]) -> list[ConversationMessageIn]:
        if not value:
            raise ValueError("messages must not be empty")
        return value


class UpdateTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value
