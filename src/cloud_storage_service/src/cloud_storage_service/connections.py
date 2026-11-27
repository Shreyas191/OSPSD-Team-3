"""A person's provider connections: stored encrypted, used per request (#98; threats T10, T13).

**Storage.** Each ``provider_connections`` row keeps the provider's secret (a refresh token,
or Trello's token and list) encrypted with Fernet under ``TOKEN_ENCRYPTION_KEY`` (secret
``token-encryption-key-<env>``, readable only by the API service). The encrypted value also
names the user and the provider, and is refused under any other row's user or provider, so
copying ciphertext between rows gives nothing. Secrets are decrypted only inside a request
that needs that provider, and never reach a response, a log line or the agent.

**Use.** Product routes take :data:`UserStorage`, :data:`UserIssues` and :data:`UserCalendar`.
Each builds the signed-in person's own client the first time the route uses it:

- not connected → 409 ``not_connected`` ("Connect Trello first");
- connected, but the provider refused the secret, now or earlier → the connection is marked
  ``needs_attention`` and the answer is 409 ``reconnect_needed`` ("Reconnect Trello");
- connected, but a choice is missing (Trello's list) → 409 ``setup_needed``.

There is **no fallback** to our test accounts or another user's connection (T13). The one
exception is a development user (``X-Dev-User``, never on the demo service), who has no
connections and runs on the service's shared clients, which are fakes or our test accounts.
"""

import functools
import json
import logging
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from calendar_contract import CalendarService
from cloud_storage_api import DocumentStorage
from course_autopilot_store import Database
from course_autopilot_store.models import ProviderConnection, User
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, Request
from issue_tracker_contract import IssueProvider
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cloud_storage_service.configuration import ConfigurationError
from cloud_storage_service.connectors import (
    Connector,
    Grant,
    ReconnectNeededError,
    SetupNeededError,
)
from cloud_storage_service.dependencies import Calendar, DocumentStore, IssueTracker
from cloud_storage_service.identity import DEV_AUTH_PROVIDER, CurrentUser, RequiredDatabase

logger = logging.getLogger(__name__)

TOKEN_KEY_ENV_VAR = "TOKEN_ENCRYPTION_KEY"  # noqa: S105 (a setting name)
NOT_CONFIGURED = "connections_not_configured"
NOT_CONFIGURED_MESSAGE = "Provider connections aren't set up on this service"
# provider_connections.status
CONNECTED = "connected"
NEEDS_ATTENTION = "needs_attention"
SETUP_NEEDED = "setup_needed"
TITLES = {"dropbox": "Dropbox", "google_calendar": "Google Calendar", "trello": "Trello"}


class ProviderNotReadyError(Exception):
    """A route needs a provider the person can't use yet: 409, or 503 if not configured."""

    def __init__(self, provider: str, code: str, message: str) -> None:
        """Record which provider, the shared error code and the message to show."""
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.message = message


def _not_connected(provider: str) -> ProviderNotReadyError:
    return ProviderNotReadyError(provider, "not_connected", f"Connect {TITLES[provider]} first")


def _reconnect(provider: str) -> ProviderNotReadyError:
    title = TITLES[provider]
    return ProviderNotReadyError(provider, "reconnect_needed", f"Reconnect {title}, then try again")


def _setup(provider: str) -> ProviderNotReadyError:
    return ProviderNotReadyError(
        provider,
        "setup_needed",
        "Choose the Trello list for your cards on the connections page",
    )


class ConnectionsNotConfiguredError(Exception):
    """No token encryption key is set, so the service can't use people's connections."""


class TokenBox:
    """Encrypts and decrypts connection secrets, each bound to its user and provider."""

    def __init__(self, key: str) -> None:
        """Use a Fernet key (32 bytes, URL-safe base64).

        Raises:
            ConfigurationError: If the key isn't a Fernet key. The message names the setting,
                never the value.

        """
        try:
            self._fernet = Fernet(key.encode())
        except ValueError:
            msg = f"{TOKEN_KEY_ENV_VAR} isn't a valid Fernet key"
            raise ConfigurationError(msg) from None

    @classmethod
    def from_env(cls) -> "TokenBox | None":
        """Return the box for ``TOKEN_ENCRYPTION_KEY``, or ``None`` when it isn't set."""
        key = os.environ.get(TOKEN_KEY_ENV_VAR, "").strip()
        return cls(key) if key else None

    def seal(self, user_id: uuid.UUID, provider: str, secret: Mapping[str, Any]) -> bytes:
        """Encrypt ``secret`` for this user and provider."""
        plain = json.dumps({"user": str(user_id), "provider": provider, "secret": dict(secret)})
        return self._fernet.encrypt(plain.encode())

    def open(self, user_id: uuid.UUID, provider: str, sealed: bytes) -> dict[str, Any]:
        """Decrypt a secret sealed for this user and provider.

        Raises:
            ValueError: If it doesn't decrypt under this key, or was sealed for another
                user or provider.

        """
        try:
            plain = json.loads(self._fernet.decrypt(sealed))
        except (InvalidToken, ValueError):
            msg = "The stored connection can't be decrypted"
            raise ValueError(msg) from None
        if plain.get("user") != str(user_id) or plain.get("provider") != provider:
            msg = "The stored connection belongs to another user or provider"
            raise ValueError(msg)
        secret: dict[str, Any] = plain["secret"]
        return secret


@dataclass(frozen=True)
class Connections:
    """What a service needs to use people's connections: the key, and each configured flow."""

    box: TokenBox
    connectors: Mapping[str, Connector]


def find_connections(request: Request) -> Connections | None:
    """Return the service's connections setup, or ``None`` when it has none."""
    connections: Connections | None = getattr(request.app.state, "connections", None)
    return connections


def get_connections(
    connections: Annotated[Connections | None, Depends(find_connections)],
) -> Connections:
    """Return the service's connections setup, or answer 503 when it has none."""
    if connections is None:
        raise ConnectionsNotConfiguredError
    return connections


# ----- Rows -----


def get_row(session: Session, user_id: uuid.UUID, provider: str) -> ProviderConnection | None:
    """Return this user's connection to ``provider``, if any."""
    return session.scalars(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == provider,
        ),
    ).first()


def save_grant(  # noqa: PLR0913 (the row is keyed by user and provider)
    session: Session,
    box: TokenBox,
    user_id: uuid.UUID,
    provider: str,
    grant: Grant,
    *,
    needs_setup: bool = False,
) -> ProviderConnection:
    """Store a finished connection, replacing any earlier one (reconnecting)."""
    row = get_row(session, user_id, provider)
    if row is None:
        row = ProviderConnection(user_id=user_id, provider=provider)
        session.add(row)
    row.encrypted_credentials = box.seal(user_id, provider, grant.secret)
    row.account_label = grant.account[:255]
    row.scopes = grant.scopes
    row.status = SETUP_NEEDED if needs_setup else CONNECTED
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row


def save_secret(
    session: Session,
    box: TokenBox,
    row: ProviderConnection,
    secret: Mapping[str, Any],
) -> None:
    """Replace a connection's stored secret (a refreshed token, or a chosen list)."""
    row.encrypted_credentials = box.seal(row.user_id, row.provider, secret)
    row.updated_at = datetime.now(UTC)
    session.flush()


def mark_needs_attention(database: Database, user_id: uuid.UUID, provider: str) -> None:
    """Record that the provider refused this connection; the person must reconnect."""
    with database.session() as session:
        session.execute(
            update(ProviderConnection)
            .where(ProviderConnection.user_id == user_id, ProviderConnection.provider == provider)
            .values(status=NEEDS_ATTENTION, updated_at=datetime.now(UTC)),
        )
    logger.warning("A %s connection was refused by the provider; marked for reconnect", provider)


# ----- Per-request clients -----


class UserProviders:
    """The signed-in person's provider clients for one request, each built on first use."""

    def __init__(
        self,
        database: Database,
        user_id: uuid.UUID,
        connections: Connections | None,
        shared: Mapping[str, object] | None,
    ) -> None:
        """Bind the user; ``shared`` (development users only) replaces their connections."""
        self._database = database
        self._user_id = user_id
        self._connections = connections
        self._shared = shared
        self._clients: dict[str, object] = {}
        self._cleanup = ExitStack()

    def get(self, provider: str) -> object:
        """Return this person's client for ``provider``, building it the first time.

        Raises:
            ProviderNotReadyError: If it isn't connected, needs reconnecting or setup, or
                this service can't use connections at all (``connections_not_configured``).

        """
        if provider not in self._clients:
            self._clients[provider] = self._build(provider)
        return self._clients[provider]

    def _stored_secret(self, provider: str, box: TokenBox) -> tuple[uuid.UUID, dict[str, Any]]:
        """Return the connection's row ID and decrypted secret, if it can be used now."""
        with self._database.session() as session:
            row = get_row(session, self._user_id, provider)
            if row is None:
                raise _not_connected(provider)
            if row.status == SETUP_NEEDED:
                raise _setup(provider)
            if row.status != CONNECTED:
                raise _reconnect(provider)
            row_id = row.id
            try:
                return row_id, box.open(self._user_id, provider, row.encrypted_credentials)
            except ValueError:
                pass
        # Unreadable (a different key, or another row's value): only reconnecting helps.
        mark_needs_attention(self._database, self._user_id, provider)
        raise _reconnect(provider)

    def _build(self, provider: str) -> object:
        if self._shared is not None:
            return self._shared[provider]
        if self._connections is None:
            raise ProviderNotReadyError(provider, NOT_CONFIGURED, NOT_CONFIGURED_MESSAGE)
        box = self._connections.box
        connector = self._connections.connectors.get(provider)
        if connector is None:
            raise _not_connected(provider)
        row_id, secret = self._stored_secret(provider, box)

        def save(fresh: dict[str, Any]) -> None:
            with self._database.session() as session:
                current = session.get(ProviderConnection, row_id)
                if current is not None:
                    save_secret(session, box, current, fresh)

        try:
            return connector.build(secret, save, self._cleanup)
        except ReconnectNeededError:
            mark_needs_attention(self._database, self._user_id, provider)
            raise _reconnect(provider) from None
        except SetupNeededError:
            raise _setup(provider) from None

    def refused(self, provider: str, error: BaseException) -> bool:
        """Whether ``error`` means the provider refused this person's connection.

        If so, the connection is marked ``needs_attention``. Never true for a development
        user's shared clients.
        """
        if self._shared is not None or self._connections is None:
            return False
        connector = self._connections.connectors.get(provider)
        if connector is None or not connector.is_auth_failure(error):
            return False
        mark_needs_attention(self._database, self._user_id, provider)
        return True

    def close(self) -> None:
        """Remove anything a client needed only for this request (Team 5's token file)."""
        self._cleanup.close()


class _OnFirstUse:
    """Stands in for one provider's client; builds it when a method is first called.

    A call the provider refuses because of the connection becomes ``reconnect_needed``,
    after the connection is marked, so every caller handles it the same way.
    """

    def __init__(self, providers: UserProviders, provider: str) -> None:
        self._providers = providers
        self._provider = provider

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 (any of the contract's methods)
        target = getattr(self._providers.get(self._provider), name)
        if not callable(target):
            return target

        @functools.wraps(target)
        def call(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            try:
                return target(*args, **kwargs)
            except Exception as e:
                if self._providers.refused(self._provider, e):
                    raise _reconnect(self._provider) from None
                raise

        return call


def get_user_providers(  # noqa: PLR0913 (each argument is a FastAPI dependency)
    connections: Annotated[Connections | None, Depends(find_connections)],
    user: CurrentUser,
    database: RequiredDatabase,
    storage: DocumentStore,
    issues: IssueTracker,
    calendar: Calendar,
) -> Iterator[UserProviders]:
    """Return the person's providers for this request, and clean up after it."""
    with database.session() as session:
        provider = session.scalars(select(User.auth_provider).where(User.id == user)).first()
    shared: dict[str, object] | None = None
    if provider == DEV_AUTH_PROVIDER:
        shared = {"dropbox": storage, "trello": issues, "google_calendar": calendar}
    providers = UserProviders(database, user, connections, shared)
    try:
        yield providers
    finally:
        providers.close()


Providers = Annotated[UserProviders, Depends(get_user_providers)]


def get_user_storage(providers: Providers) -> DocumentStorage:
    """Return the person's own Dropbox storage, built when first used."""
    return cast("DocumentStorage", _OnFirstUse(providers, "dropbox"))


def get_user_issues(providers: Providers) -> IssueProvider:
    """Return the person's own Trello list, built when first used."""
    return cast("IssueProvider", _OnFirstUse(providers, "trello"))


def get_user_calendar(providers: Providers) -> CalendarService:
    """Return the person's own Google Calendar, built when first used."""
    return cast("CalendarService", _OnFirstUse(providers, "google_calendar"))


UserStorage = Annotated[DocumentStorage, Depends(get_user_storage)]
UserIssues = Annotated[IssueProvider, Depends(get_user_issues)]
UserCalendar = Annotated[CalendarService, Depends(get_user_calendar)]
