"""Course Autopilot's tables: the state the services keep between requests.

Every row a user owns carries ``user_id``, so each query can be scoped to the signed-in
user (Level 4). Provider credentials are stored only encrypted. The first migration
(``migrations/versions/0001_initial.py``) creates exactly these tables; change them only
through a new migration.

- ``users``: one per person, with the stable identity their sign-in provider gives them.
- ``provider_connections``: a user's Dropbox, Trello or Google Calendar access.
- ``conversations`` and ``messages``: the agent conversation, turn by turn.
- ``proposals``: a change set the agent proposed, and ``approvals`` of its exact content.
- ``change_outcomes``: what happened to each change when an approved proposal ran.
- ``external_records``: every provider record the product created, so it changes or
  deletes only its own records.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class of every table."""


class User(Base):
    """A person with an account. ``auth_subject`` is their sign-in provider's stable ID."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("auth_provider", "auth_subject"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    auth_provider: Mapped[str] = mapped_column(String(32))
    auth_subject: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProviderConnection(Base):
    """A user's access to one provider. At most one connection per user and provider."""

    __tablename__ = "provider_connections"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32))  # dropbox | trello | google_calendar
    account_label: Mapped[str] = mapped_column(String(255), default="")
    scopes: Mapped[str] = mapped_column(Text, default="")
    # Encrypted with a key kept outside the database; never stored in plain text.
    encrypted_credentials: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="connected")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now,
        onupdate=_now,
    )


class Conversation(Base):
    """One agent conversation, owned by one user."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Message(Base):
    """One turn of a conversation. ``seq`` orders the turns; ``content`` is its JSON body."""

    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | tool
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Proposal(Base):
    """A change set the agent proposed. ``changes_hash`` identifies its exact content."""

    __tablename__ = "proposals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
    )
    # pending | approved | rejected | executing | done | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending")
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    changes_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # While executing: when the run last showed it was alive (claimed, or began a change).
    # A run silent for too long has stopped, and another run may take it over (migration 0002).
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    """A user's approval of one proposal, bound to the hash of the content they saw."""

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("proposals.id", ondelete="CASCADE"),
        unique=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    changes_hash: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChangeOutcome(Base):
    """What happened to one change of an approved proposal.

    ``started`` is saved just before the provider write, so a run that stops before it can
    save the outcome leaves a trace; the next run turns it into ``uncertain``.
    """

    __tablename__ = "change_outcomes"
    __table_args__ = (UniqueConstraint("proposal_id", "change_index"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("proposals.id", ondelete="CASCADE"))
    change_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # started | completed | failed | uncertain
    detail: Mapped[str] = mapped_column(Text, default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ExternalRecord(Base):
    """A provider record the product created for a user (a card, an event or a document)."""

    __tablename__ = "external_records"
    __table_args__ = (UniqueConstraint("user_id", "kind", "provider_record_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    course_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16))  # issue | event | document
    provider_record_id: Mapped[str] = mapped_column(String(512))
    created_by_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
