"""Conversation storage for the agent service (issue #92).

The agent service keeps no state between requests: it loads a conversation's messages from
here at the start of a turn and appends the turn's messages at the end. The API service owns
the database, so it also owns this data, and every route acts as the signed-in user only:
another user's conversation is 404.

- ``POST /conversations``: start one.
- ``GET /conversations/{id}/messages``: every message, in order.
- ``POST /conversations/{id}/messages``: append messages; their order numbers (``seq``) are
  assigned here. Two concurrent appends can't interleave: the second gets 409 and retries.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from course_autopilot_store.models import Conversation, Message
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cloud_storage_service.errors import ErrorResponse, SharedErrorRoute, error_response
from cloud_storage_service.identity import CurrentUser, RequiredDatabase

router = APIRouter(prefix="/conversations", tags=["conversations"], route_class=SharedErrorRoute)

MAX_MESSAGES_PER_APPEND = 50
MAX_MESSAGE_BYTES = 256 * 1024

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse, "description": text}
    for code, text in {
        400: "A message is invalid or too large",
        401: "Not signed in",
        404: "No such conversation for this account",
        409: "Another append happened at the same time; reload and retry",
        503: "No database is configured",
    }.items()
}


class ConversationCreate(BaseModel):
    """Body of ``POST /conversations``."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)


class ConversationResponse(BaseModel):
    """A conversation."""

    id: uuid.UUID
    title: str
    created_at: datetime


class NewMessage(BaseModel):
    """One message to append. ``content`` is the agent's JSON body for the turn."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant", "tool"]
    content: dict[str, Any]

    @field_validator("content")
    @classmethod
    def _not_too_large(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_MESSAGE_BYTES:
            msg = f"a message's content must be at most {MAX_MESSAGE_BYTES} bytes as JSON"
            raise ValueError(msg)
        return value


class AppendMessages(BaseModel):
    """Body of ``POST /conversations/{id}/messages``."""

    model_config = ConfigDict(extra="forbid")

    messages: list[NewMessage] = Field(min_length=1, max_length=MAX_MESSAGES_PER_APPEND)


class MessageResponse(BaseModel):
    """A stored message."""

    seq: int
    role: str
    content: dict[str, Any]
    created_at: datetime


def _owned(session: Session, user: uuid.UUID, conversation_id: uuid.UUID) -> Conversation | None:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user:
        return None
    return conversation


def _not_found() -> JSONResponse:
    return error_response(404, "not_found", "Conversation not found")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
def create_conversation(
    body: ConversationCreate,
    user: CurrentUser,
    database: RequiredDatabase,
) -> ConversationResponse:
    """Start a conversation owned by the signed-in user."""
    with database.session() as session:
        conversation = Conversation(user_id=user, title=body.title)
        session.add(conversation)
        session.flush()
        return ConversationResponse(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
        )


@router.get(
    "/{conversation_id}/messages",
    responses=ERRORS,
    response_model=list[MessageResponse],
)
def list_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    database: RequiredDatabase,
) -> list[MessageResponse] | JSONResponse:
    """Return every message of this user's conversation, in order."""
    with database.session() as session:
        if _owned(session, user, conversation_id) is None:
            return _not_found()
        return [
            MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
            for m in session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq),
            )
        ]


@router.post(
    "/{conversation_id}/messages",
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
    response_model=list[MessageResponse],
)
def append_messages(
    conversation_id: uuid.UUID,
    body: AppendMessages,
    user: CurrentUser,
    database: RequiredDatabase,
) -> list[MessageResponse] | JSONResponse:
    """Append messages to this user's conversation, after the last one."""
    try:
        with database.session() as session:
            if _owned(session, user, conversation_id) is None:
                return _not_found()
            last = session.scalar(
                select(func.max(Message.seq)).where(Message.conversation_id == conversation_id),
            )
            start = 0 if last is None else last + 1
            rows = [
                Message(
                    conversation_id=conversation_id,
                    seq=start + offset,
                    role=m.role,
                    content=m.content,
                )
                for offset, m in enumerate(body.messages)
            ]
            session.add_all(rows)
            session.flush()
            return [
                MessageResponse(seq=r.seq, role=r.role, content=r.content, created_at=r.created_at)
                for r in rows
            ]
    except IntegrityError:
        # The unique (conversation_id, seq) row: another append took these numbers first.
        return error_response(409, "conflict", "Another append happened at the same time")
