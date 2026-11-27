"""Connecting, checking and disconnecting a person's providers over HTTP (#98-#101).

Every route needs the person's own session: an agent token gets 403 (DESIGN.md P1, T14).

- ``GET /connections``: each provider, whether it can be connected here, and its state:
  ``not_connected``, ``connected``, ``needs_attention`` (reconnect) or ``setup_needed``.
- ``POST /connections/{provider}/start``: where to send the browser. The attempt belongs to
  this session and is used once, so a callback finished by another session fails (T9).
- ``POST /connections/{provider}/finish``: what the provider sent back (``state`` and
  ``code``; for Trello, ``code`` is the token from the return URL's fragment). Connecting
  again replaces the stored secret.
- ``DELETE /connections/{provider}``: deletes our stored secret, then revokes it at the
  provider. A failed revoke is logged; the secret is already gone from our side.
- ``GET /connections/trello/lists`` and ``PUT /connections/trello/list``: the person's open
  Trello lists, and the one their cards go to.

No response ever contains a token (T10).
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from course_autopilot_store.models import ConnectionAttempt, ProviderConnection
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from cloud_storage_service.connections import (
    TITLES,
    Connections,
    get_connections,
    get_row,
    mark_needs_attention,
    save_grant,
    save_secret,
)
from cloud_storage_service.connectors import (
    Connector,
    ConnectorError,
    ReconnectNeededError,
    TrelloConnector,
)
from cloud_storage_service.errors import ErrorResponse, SharedErrorRoute, error_response
from cloud_storage_service.identity import Person, RequiredDatabase, hash_token
from cloud_storage_service.sign_in import pkce_challenge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connections", tags=["connections"], route_class=SharedErrorRoute)

ATTEMPT_LIFETIME = timedelta(minutes=10)
ProviderName = Literal["dropbox", "google_calendar", "trello"]
ConnectionsSetup = Annotated[Connections, Depends(get_connections)]

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse, "description": text}
    for code, text in {
        400: "The connection didn't complete, or the request is malformed",
        401: "Not signed in",
        403: "An agent token can't do this",
        409: "Not connected, or a development user",
        503: "Connections aren't set up here, or the provider is unreachable",
    }.items()
}


class ConnectionView(BaseModel):
    """One provider as the connections page shows it. Never contains a token."""

    provider: str
    title: str
    available: bool
    status: str
    account: str = ""
    updated_at: datetime | None = None


class StartResponse(BaseModel):
    """Where to send the browser."""

    authorization_url: str


_VALUE = Field(min_length=1, max_length=4096)


class FinishRequest(BaseModel):
    """What the provider sent back to the interface."""

    model_config = ConfigDict(extra="forbid")

    state: str = _VALUE
    code: str = _VALUE


class TrelloListView(BaseModel):
    """A Trello list the person can choose."""

    board: str
    id: str
    name: str


class ChooseListRequest(BaseModel):
    """The list the person's cards go to."""

    model_config = ConfigDict(extra="forbid")

    list_id: str = Field(min_length=1, max_length=64)


def _view(provider: str, row: ProviderConnection | None, setup: Connections) -> ConnectionView:
    return ConnectionView(
        provider=provider,
        title=TITLES[provider],
        available=provider in setup.connectors,
        status=row.status if row is not None else "not_connected",
        account=row.account_label if row is not None else "",
        updated_at=row.updated_at if row is not None else None,
    )


def _connector(setup: Connections, provider: str) -> Connector | JSONResponse:
    connector = setup.connectors.get(provider)
    if connector is None:
        message = f"Connecting {TITLES[provider]} isn't set up on this service"
        return error_response(503, "connection_not_configured", message)
    return connector


def _session_id(person: Any) -> uuid.UUID | JSONResponse:  # noqa: ANN401 (a Caller)
    if person.session_id is None:
        return error_response(
            409,
            "conflict",
            "A development user runs on this service's shared test accounts",
        )
    session_id: uuid.UUID = person.session_id
    return session_id


def _failed(title: str) -> JSONResponse:
    return error_response(400, "connect_failed", f"{title} didn't connect; try again")


@router.get("", responses=ERRORS)
def list_connections(
    person: Person,
    database: RequiredDatabase,
    setup: ConnectionsSetup,
) -> list[ConnectionView]:
    """List each provider and this person's connection to it."""
    with database.session() as session:
        return [
            _view(provider, get_row(session, person.user_id, provider), setup)
            for provider in TITLES
        ]


@router.post("/{provider}/start", responses=ERRORS, response_model=StartResponse)
def start(
    provider: ProviderName,
    person: Person,
    database: RequiredDatabase,
    setup: ConnectionsSetup,
) -> StartResponse | JSONResponse:
    """Begin connecting a provider."""
    connector = _connector(setup, provider)
    session_id = _session_id(person)
    if isinstance(connector, JSONResponse):
        return connector
    if isinstance(session_id, JSONResponse):
        return session_id
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    now = datetime.now(UTC)
    with database.session() as session:
        session.execute(
            delete(ConnectionAttempt).where(ConnectionAttempt.created_at < now - ATTEMPT_LIFETIME),
        )
        session.add(
            ConnectionAttempt(
                session_id=session_id,
                provider=provider,
                state_hash=hash_token(state),
                code_verifier=verifier,
                created_at=now,
            ),
        )
    return StartResponse(
        authorization_url=connector.authorization_url(state, pkce_challenge(verifier)),
    )


def _use_attempt(
    session: Session,
    session_id: uuid.UUID,
    provider: str,
    state: str,
) -> str | None:
    """Use the attempt for ``state`` once; return its PKCE verifier, or None.

    It returns instead of raising, so the attempt stays used whatever the outcome.
    """
    now = datetime.now(UTC)
    attempt = session.scalars(
        select(ConnectionAttempt).where(ConnectionAttempt.state_hash == hash_token(state)),
    ).first()
    if attempt is None:
        return None
    created = attempt.created_at
    created = created if created.tzinfo else created.replace(tzinfo=UTC)
    used = session.execute(
        update(ConnectionAttempt)
        .where(ConnectionAttempt.id == attempt.id, ConnectionAttempt.used_at.is_(None))
        .values(used_at=now),
    )
    if (
        used.rowcount != 1  # type: ignore[attr-defined]  # a CursorResult for an UPDATE
        or now - created > ATTEMPT_LIFETIME
        or attempt.session_id != session_id
        or attempt.provider != provider
    ):
        return None
    return attempt.code_verifier


@router.post("/{provider}/finish", responses=ERRORS, response_model=ConnectionView)
def finish(
    provider: ProviderName,
    body: FinishRequest,
    person: Person,
    database: RequiredDatabase,
    setup: ConnectionsSetup,
) -> ConnectionView | JSONResponse:
    """Finish connecting a provider and store the connection, encrypted."""
    connector = _connector(setup, provider)
    session_id = _session_id(person)
    if isinstance(connector, JSONResponse):
        return connector
    if isinstance(session_id, JSONResponse):
        return session_id
    with database.session() as session:
        verifier = _use_attempt(session, session_id, provider, body.state)
    if verifier is None:
        return _failed(connector.title)
    try:
        grant = connector.finish(body.code, verifier)
    except ConnectorError as e:
        return error_response(e.status, e.code, e.message)
    with database.session() as session:
        row = save_grant(
            session,
            setup.box,
            person.user_id,
            provider,
            grant,
            needs_setup=connector.needs_setup(grant.secret),
        )
        return _view(provider, row, setup)


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
def disconnect(
    provider: ProviderName,
    person: Person,
    database: RequiredDatabase,
    setup: ConnectionsSetup,
) -> Response:
    """Delete our stored secret for this provider, then revoke it at the provider."""
    secret: dict[str, Any] | None = None
    with database.session() as session:
        row = get_row(session, person.user_id, provider)
        if row is not None:
            try:
                secret = setup.box.open(person.user_id, provider, row.encrypted_credentials)
            except ValueError:
                secret = None  # Unreadable: nothing we can revoke, but still delete it.
            session.delete(row)
    connector = setup.connectors.get(provider)
    if secret is not None and connector is not None:
        try:
            connector.revoke(secret)
        except ConnectorError:
            logger.warning("Revoking a %s connection at the provider failed", provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _trello(setup: Connections) -> TrelloConnector | JSONResponse:
    connector = _connector(setup, "trello")
    if isinstance(connector, JSONResponse) or not isinstance(connector, TrelloConnector):
        return error_response(503, "connection_not_configured", "Connecting Trello isn't set up")
    return connector


def _trello_secret(
    database: RequiredDatabase,
    setup: Connections,
    user_id: uuid.UUID,
) -> dict[str, Any] | JSONResponse:
    with database.session() as session:
        row = get_row(session, user_id, "trello")
        if row is None:
            return error_response(409, "not_connected", "Connect Trello first")
        try:
            return setup.box.open(user_id, "trello", row.encrypted_credentials)
        except ValueError:
            return error_response(409, "reconnect_needed", "Reconnect Trello, then try again")


def _trello_refused(database: RequiredDatabase, user_id: uuid.UUID) -> JSONResponse:
    mark_needs_attention(database, user_id, "trello")
    return error_response(409, "reconnect_needed", "Reconnect Trello, then try again")


@router.get("/trello/lists", responses=ERRORS, response_model=list[TrelloListView])
def trello_lists(
    person: Person,
    database: RequiredDatabase,
    setup: ConnectionsSetup,
) -> list[TrelloListView] | JSONResponse:
    """List the open lists on the person's open Trello boards."""
    connector = _trello(setup)
    if isinstance(connector, JSONResponse):
        return connector
    secret = _trello_secret(database, setup, person.user_id)
    if isinstance(secret, JSONResponse):
        return secret
    try:
        found = connector.lists(secret)
    except ReconnectNeededError:
        return _trello_refused(database, person.user_id)
    except ConnectorError as e:
        return error_response(e.status, e.code, e.message)
    return [TrelloListView(board=item.board, id=item.id, name=item.name) for item in found]


@router.put("/trello/list", responses=ERRORS, response_model=ConnectionView)
def choose_trello_list(
    body: ChooseListRequest,
    person: Person,
    database: RequiredDatabase,
    setup: ConnectionsSetup,
) -> ConnectionView | JSONResponse:
    """Choose the list the person's cards go to."""
    connector = _trello(setup)
    if isinstance(connector, JSONResponse):
        return connector
    secret = _trello_secret(database, setup, person.user_id)
    if isinstance(secret, JSONResponse):
        return secret
    try:
        chosen = connector.choose_list(secret, body.list_id)
    except ReconnectNeededError:
        return _trello_refused(database, person.user_id)
    except ConnectorError as e:
        return error_response(e.status, e.code, e.message)
    with database.session() as session:
        row = get_row(session, person.user_id, "trello")
        if row is None:
            return error_response(409, "not_connected", "Connect Trello first")
        save_secret(session, setup.box, row, chosen)
        if row.status != "needs_attention":
            row.status = "connected"
        return _view("trello", row, setup)
