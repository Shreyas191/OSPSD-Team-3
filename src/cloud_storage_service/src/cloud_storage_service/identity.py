"""Who is calling a product route, and what they may do (#96, #97; HW3 Milestone 2).

Every product route takes its user from here, never from a request body or a tool argument.
A caller proves who they are in one of two ways:

- ``Authorization: Bearer <token>``: a session from Google sign-in (``sign_in.py``), or an
  agent token made from one. Only the token's SHA-256 is stored, in ``sessions``.
- ``X-Dev-User: <name>``: a development user, accepted only when ``DEV_SIGN_IN=enabled`` on a
  service other than demo, so tests and the test deployment can run without Google.

| Token | Lasts | May |
|---|---|---|
| ``session`` | 24 hours idle, 7 days in all; ended by sign-out | Everything the person may do |
| ``agent`` | 5 minutes, and never past its session | Read and propose only (``CurrentUser``) |

Routes that change a provider or the account (approve, reject, execute, sign-out, making
agent tokens) take ``Person``, which refuses an agent token with 403. So the agent service,
which only ever holds an agent token, can't approve anything even if it is compromised
(DESIGN.md P1, threat T14). Anything else, a missing, unknown or expired token, is 401.
"""

import hashlib
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from course_autopilot_store import Database
from course_autopilot_store.models import User, UserSession
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from cloud_storage_service.database import DatabaseDependency
from cloud_storage_service.faults import DEMO_SERVICE

DEV_SIGN_IN_ENV_VAR = "DEV_SIGN_IN"
DEV_AUTH_PROVIDER = "dev"
_DEV_SUBJECT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

SESSION_IDLE = timedelta(hours=24)
SESSION_LIFETIME = timedelta(days=7)
AGENT_TOKEN_LIFETIME = timedelta(minutes=5)
# A session's last use is written at most this often, not on every request.
TOUCH_EVERY = timedelta(minutes=5)

SIGN_IN_MESSAGE = "Sign in to continue"
AGENT_REFUSED = "Only you can do this, from the Course Autopilot page; the assistant can't"


def dev_sign_in_enabled() -> bool:
    """Whether ``X-Dev-User`` is accepted: opted in, and never on the demo service."""
    return (
        os.environ.get(DEV_SIGN_IN_ENV_VAR) == "enabled"
        and os.environ.get("K_SERVICE") != DEMO_SERVICE
    )


def require_database(database: DatabaseDependency) -> Database:
    """Return the database, or answer 503 when the service has none."""
    if database is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No database is configured")
    return database


RequiredDatabase = Annotated[Database, Depends(require_database)]


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a token: what the database keeps instead of the token."""
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    # SQLite gives back naive datetimes; every time this module writes is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Caller:
    """The signed-in user, the kind of token they used, and its session (none for dev)."""

    user_id: uuid.UUID
    scope: str  # session | agent
    session_id: uuid.UUID | None = None


def create_session(session: Session, user_id: uuid.UUID) -> tuple[str, datetime]:
    """Start a session for ``user_id``; return its token (shown once) and when it ends."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires = now + SESSION_LIFETIME
    session.add(
        UserSession(
            user_id=user_id,
            token_hash=hash_token(token),
            scope="session",
            created_at=now,
            last_seen_at=now,
            expires_at=expires,
        ),
    )
    return token, expires


def create_agent_token(session: Session, caller: Caller) -> tuple[str, datetime]:
    """Make a read-and-propose token for one agent turn, ending with its session."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires = now + AGENT_TOKEN_LIFETIME
    if caller.session_id is not None:
        parent = session.get(UserSession, caller.session_id)
        if parent is not None:
            expires = min(expires, _aware(parent.expires_at))
    session.add(
        UserSession(
            user_id=caller.user_id,
            token_hash=hash_token(token),
            scope="agent",
            parent_id=caller.session_id,
            created_at=now,
            last_seen_at=now,
            expires_at=expires,
        ),
    )
    return token, expires


def end_session(session: Session, caller: Caller) -> None:
    """Sign out: delete this session and the agent tokens made from it."""
    if caller.session_id is not None:
        sid = caller.session_id
        session.execute(
            delete(UserSession).where(or_(UserSession.id == sid, UserSession.parent_id == sid)),
        )


def end_every_session(session: Session, user_id: uuid.UUID) -> None:
    """Sign out everywhere: delete every session and agent token of this user."""
    session.execute(delete(UserSession).where(UserSession.user_id == user_id))


def _from_token(database: Database, token: str) -> Caller:
    now = datetime.now(UTC)
    with database.session() as session:
        row = session.scalars(
            select(UserSession).where(UserSession.token_hash == hash_token(token)),
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, SIGN_IN_MESSAGE)
        idle = row.scope == "session" and now - _aware(row.last_seen_at) > SESSION_IDLE
        if now >= _aware(row.expires_at) or idle:
            session.delete(row)
            # Raised after the with block, so the deletion is committed.
            caller = None
        else:
            if now - _aware(row.last_seen_at) > TOUCH_EVERY:
                row.last_seen_at = now
            caller = Caller(row.user_id, row.scope, row.id)
    if caller is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, SIGN_IN_MESSAGE)
    return caller


def _from_dev_header(database: Database, name: str) -> Caller:
    if not _DEV_SUBJECT.fullmatch(name):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, SIGN_IN_MESSAGE)
    with database.session() as session:
        user = session.scalars(
            select(User).where(
                User.auth_provider == DEV_AUTH_PROVIDER,
                User.auth_subject == name,
            ),
        ).first()
        if user is None:
            user = User(auth_provider=DEV_AUTH_PROVIDER, auth_subject=name)
            session.add(user)
            session.flush()
        return Caller(user.id, "session")


def get_caller(
    database: RequiredDatabase,
    authorization: Annotated[str | None, Header()] = None,
    x_dev_user: Annotated[str | None, Header()] = None,
) -> Caller:
    """Return who is calling, from the bearer token or the dev header; otherwise 401."""
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, SIGN_IN_MESSAGE)
        return _from_token(database, token.strip())
    if x_dev_user is not None and dev_sign_in_enabled():
        return _from_dev_header(database, x_dev_user)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, SIGN_IN_MESSAGE)


AnyCaller = Annotated[Caller, Depends(get_caller)]


def get_current_user(caller: AnyCaller) -> uuid.UUID:
    """Return the acting user's ID; an agent token is enough (reads and proposals)."""
    return caller.user_id


def get_person(caller: AnyCaller) -> Caller:
    """Return the caller if they used their own session; an agent token is refused (403)."""
    if caller.scope != "session":
        raise HTTPException(status.HTTP_403_FORBIDDEN, AGENT_REFUSED)
    return caller


CurrentUser = Annotated[uuid.UUID, Depends(get_current_user)]
Person = Annotated[Caller, Depends(get_person)]
