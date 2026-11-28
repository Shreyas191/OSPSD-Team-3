"""How each provider is connected, used and disconnected for one person (#99, #100, #101).

Each :class:`Connector` knows one provider's authorization flow, turns a finished one into a
:class:`Grant` (the secret to store encrypted, and the account to show), builds that
person's client from the secret on each request, and revokes the secret on disconnect:

- ``dropbox``: OAuth code with PKCE for an offline (refresh) token, through our App folder
  app; each request gets ``DropboxDocumentStorage(DropboxClient(dbx=...))``, built in
  ``dropbox_client_impl.connect``.
- ``google_calendar``: OAuth code with PKCE for ``calendar.events``, a consent separate from
  sign-in; each request gets Team 5's ``create_calendar_service(token_file=...)`` on a token
  file that exists only for that request.
- ``trello``: Trello's authorize page returns a token, then the person picks a list; each
  request gets Team 4's ``TrelloIssueProvider(api_key=..., token=..., list_id=...)``.

Each connector is on only when its settings are present (``build_connectors``). Every
argument is given explicitly, so no provider package falls back to our test accounts in its
environment variables (threat T13). No connector puts a token in a URL, a log line or an
error message (threat T10).
"""

import json
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self
from urllib.parse import urlencode

import httpx
from calendar_contract import CalendarError, CalendarService, ErrorCode
from dropbox_client_impl import DropboxDocumentStorage, DropboxError
from dropbox_client_impl import auth as dropbox_auth
from dropbox_client_impl import connect as dropbox_connect
from issue_tracker_contract.errors import ErrorCode as IssueErrorCode
from issue_tracker_contract.errors import ServiceError

from cloud_storage_service.sign_in import (
    AUTHORIZATION_ENDPOINT,
    CLIENT_ID_ENV_VAR,
    CLIENT_SECRET_ENV_VAR,
    TOKEN_ENDPOINT,
    TokenVerifier,
    verify_with_google,
)

if TYPE_CHECKING:
    from issue_tracker_contract import IssueProvider

logger = logging.getLogger(__name__)
# httpx logs each request URL at INFO. Trello's revoke URL contains the token, so keep
# httpx's own lines to warnings and above (T10).
logging.getLogger("httpx").setLevel(logging.WARNING)

CALENDAR_REDIRECT_ENV_VAR = "CALENDAR_CONNECT_REDIRECT_URI"
TRELLO_KEY_ENV_VAR = "TRELLO_CONNECT_API_KEY"
TRELLO_RETURN_ENV_VAR = "TRELLO_CONNECT_RETURN_URL"

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
# A stored access token is refreshed when it has less than this left, before a request.
REFRESH_MARGIN = timedelta(minutes=5)

TRELLO_AUTHORIZE = "https://trello.com/1/authorize"
TRELLO_API = "https://api.trello.com/1"
APP_NAME = "Course Autopilot"


class ConnectorError(Exception):
    """Connecting failed. ``status`` is 400 (refused, or wrong input) or 503 (unreachable)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Record the HTTP status, the shared error code and a message with no secrets."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _refused(title: str) -> ConnectorError:
    return ConnectorError(400, "connect_failed", f"{title} didn't connect; try again")


def _unreachable(title: str) -> ConnectorError:
    return ConnectorError(503, "provider_unavailable", f"{title} can't be reached; try again")


class ReconnectNeededError(Exception):
    """The provider no longer accepts the stored secret; the person must reconnect."""


class SetupNeededError(Exception):
    """Connected, but a choice is still missing (Trello's list)."""


@dataclass(frozen=True)
class Grant:
    """A finished connection: the secret to store encrypted, and what the page shows."""

    secret: dict[str, Any]
    account: str
    scopes: str


# Saves a refreshed secret back to the connection (encrypted) during a request.
SaveSecret = Callable[[dict[str, Any]], None]


class Connector(Protocol):
    """One provider's connection flow and per-request client."""

    provider: str
    title: str

    def authorization_url(self, state: str, code_challenge: str) -> str:
        """Return where to send the browser for this ``state`` (and PKCE challenge)."""
        ...

    def finish(self, code: str, code_verifier: str) -> Grant:
        """Turn what the provider sent back into a grant; raise ConnectorError if not."""
        ...

    def needs_setup(self, secret: Mapping[str, Any]) -> bool:
        """Whether a choice is still missing before the connection can be used."""
        ...

    def build(self, secret: dict[str, Any], save: SaveSecret, cleanup: ExitStack) -> object:
        """Return this person's client; raise ReconnectNeededError or SetupNeededError."""
        ...

    def is_auth_failure(self, error: BaseException) -> bool:
        """Whether ``error`` from the client means the provider refused the secret."""
        ...

    def revoke(self, secret: Mapping[str, Any]) -> None:
        """End the secret at the provider; raise ConnectorError if that fails."""
        ...


# ----- Dropbox (#99) -----


class DropboxConnector:
    """A person's own Dropbox, through our App folder app."""

    provider = "dropbox"
    title = "Dropbox"

    def __init__(self, settings: dropbox_auth.ConnectSettings) -> None:
        """Bind the Dropbox app and redirect URI."""
        self._settings = settings

    def authorization_url(self, state: str, code_challenge: str) -> str:
        """Return Dropbox's authorization URL."""
        return dropbox_connect.authorization_url(self._settings, state, code_challenge)

    def finish(self, code: str, code_verifier: str) -> Grant:
        """Exchange the code for a refresh token."""
        try:
            grant = dropbox_connect.exchange_code(self._settings, code, code_verifier)
        except dropbox_auth.DropboxConnectionError:
            raise _unreachable(self.title) from None
        except DropboxError:
            raise _refused(self.title) from None
        return Grant(
            secret={"refresh_token": grant.refresh_token, "account_id": grant.account_id},
            account=grant.email,
            scopes=grant.scopes,
        )

    def needs_setup(self, _secret: Mapping[str, Any]) -> bool:
        """Nothing to choose: the App folder is the person's storage."""
        return False

    def build(self, secret: dict[str, Any], _save: SaveSecret, _cleanup: ExitStack) -> object:
        """Return storage acting as the person; the SDK refreshes access tokens itself."""
        client = dropbox_connect.client_for(self._settings, str(secret["refresh_token"]))
        return DropboxDocumentStorage(client)

    def is_auth_failure(self, error: BaseException) -> bool:
        """Whether Dropbox refused the token."""
        return dropbox_connect.is_auth_failure(error)

    def revoke(self, secret: Mapping[str, Any]) -> None:
        """Revoke the refresh token at Dropbox."""
        try:
            dropbox_connect.revoke(self._settings, str(secret["refresh_token"]))
        except DropboxError:
            raise _unreachable(self.title) from None


# ----- Google Calendar (#100) -----


@dataclass(frozen=True)
class GoogleCalendarSettings:
    """Our Google OAuth client (shared with sign-in) and the calendar redirect URI."""

    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls) -> Self | None:
        """Read the three settings; ``None`` (calendar connections off) unless all are set."""
        names = (CLIENT_ID_ENV_VAR, CLIENT_SECRET_ENV_VAR, CALENDAR_REDIRECT_ENV_VAR)
        values = [os.environ.get(name, "").strip() for name in names]
        return cls(*values) if all(values) else None


def _expiry(expires_in: object, now: datetime) -> str:
    seconds = expires_in if isinstance(expires_in, int) else 0
    return (now + timedelta(seconds=seconds)).isoformat()


def _write_token_file(directory: Path, info: Mapping[str, Any]) -> Path:
    path = directory / "token.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(info, f)
    return path


class GoogleCalendarConnector:
    """A person's own Google Calendar, with only the ``calendar.events`` scope.

    It is a separate consent from sign-in. ``openid email`` are asked for too, only so the
    connections page can show which Google account the calendar belongs to.
    """

    provider = "google_calendar"
    title = "Google Calendar"

    def __init__(
        self,
        settings: GoogleCalendarSettings,
        http: httpx.Client,
        verify: TokenVerifier = verify_with_google,
    ) -> None:
        """Bind the OAuth client, an HTTP client for Google, and the ID token check."""
        self._settings = settings
        self._http = http
        self._verify = verify

    def authorization_url(self, state: str, code_challenge: str) -> str:
        """Return Google's consent URL for an offline ``calendar.events`` grant."""
        query = urlencode(
            {
                "client_id": self._settings.client_id,
                "redirect_uri": self._settings.redirect_uri,
                "response_type": "code",
                "scope": f"openid email {CALENDAR_SCOPE}",
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    def _token_request(self, data: dict[str, str]) -> httpx.Response:
        try:
            return self._http.post(TOKEN_ENDPOINT, data=data)
        except httpx.HTTPError:
            raise _unreachable(self.title) from None

    def finish(self, code: str, code_verifier: str) -> Grant:
        """Exchange the code; require a refresh token and the ``calendar.events`` scope."""
        now = datetime.now(UTC)
        response = self._token_request(
            {
                "code": code,
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "redirect_uri": self._settings.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        body = response.json() if response.status_code == httpx.codes.OK else {}
        scopes = str(body.get("scope") or "")
        if not body.get("refresh_token") or CALENDAR_SCOPE not in scopes.split():
            raise ConnectorError(
                400,
                "connect_failed",
                "Google Calendar didn't connect; allow calendar access and try again",
            )
        email = ""
        if body.get("id_token"):
            try:
                email = str(self._verify(str(body["id_token"]), self._settings.client_id)["email"])
            except (ValueError, KeyError):
                email = ""
        return Grant(
            secret={
                "refresh_token": str(body["refresh_token"]),
                "access_token": str(body.get("access_token") or ""),
                "expiry": _expiry(body.get("expires_in"), now),
            },
            account=email,
            scopes=scopes,
        )

    def needs_setup(self, _secret: Mapping[str, Any]) -> bool:
        """Nothing to choose: events go to the primary calendar."""
        return False

    def _fresh(self, secret: dict[str, Any], save: SaveSecret) -> dict[str, Any]:
        """Return the secret with an access token valid for a while, refreshing it if not."""
        now = datetime.now(UTC)
        expiry = datetime.fromisoformat(str(secret.get("expiry") or now.isoformat()))
        if secret.get("access_token") and expiry - now > REFRESH_MARGIN:
            return secret
        response = self._token_request(
            {
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "refresh_token": str(secret["refresh_token"]),
                "grant_type": "refresh_token",
            },
        )
        if response.status_code in {httpx.codes.BAD_REQUEST, httpx.codes.UNAUTHORIZED}:
            raise ReconnectNeededError  # invalid_grant: revoked, or expired (Testing mode)
        if response.status_code != httpx.codes.OK:
            raise _unreachable(self.title)
        body = response.json()
        fresh = {
            **secret,
            "access_token": str(body.get("access_token") or ""),
            "expiry": _expiry(body.get("expires_in"), now),
        }
        save(fresh)
        return fresh

    def build(self, secret: dict[str, Any], save: SaveSecret, cleanup: ExitStack) -> object:
        """Return Team 5's calendar on a token file that exists only for this request."""
        from team5_calendar import create_calendar_service  # noqa: PLC0415

        fresh = self._fresh(secret, save)
        directory = Path(tempfile.mkdtemp(prefix="calendar-"))
        cleanup.callback(shutil.rmtree, directory, ignore_errors=True)
        expiry = datetime.fromisoformat(str(fresh["expiry"])).astimezone(UTC)
        path = _write_token_file(
            directory,
            {
                "token": fresh["access_token"],
                "refresh_token": fresh["refresh_token"],
                "token_uri": TOKEN_ENDPOINT,
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "scopes": [CALENDAR_SCOPE],
                # google-auth reads a naive UTC time in this format.
                "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        calendar: CalendarService = create_calendar_service(token_file=path)
        return calendar

    def is_auth_failure(self, error: BaseException) -> bool:
        """Team 5 reports a refused token as a non-retryable ``provider_unavailable``."""
        return (
            isinstance(error, CalendarError)
            and error.code == ErrorCode.PROVIDER_UNAVAILABLE
            and not error.retryable
        )

    def revoke(self, secret: Mapping[str, Any]) -> None:
        """Revoke the refresh token (and its access tokens) at Google."""
        try:
            response = self._http.post(
                GOOGLE_REVOKE_ENDPOINT,
                data={"token": str(secret["refresh_token"])},
            )
        except httpx.HTTPError:
            raise _unreachable(self.title) from None
        # 400 means Google no longer knows the token: already revoked or expired.
        if response.status_code not in {httpx.codes.OK, httpx.codes.BAD_REQUEST}:
            raise _unreachable(self.title)


# ----- Trello (#101) -----


@dataclass(frozen=True)
class TrelloSettings:
    """Our Trello Power-Up's API key, and where Trello sends the browser back."""

    api_key: str
    return_url: str

    @classmethod
    def from_env(cls) -> Self | None:
        """Read both settings; ``None`` (Trello connections off) unless both are set."""
        values = [
            os.environ.get(n, "").strip() for n in (TRELLO_KEY_ENV_VAR, TRELLO_RETURN_ENV_VAR)
        ]
        return cls(*values) if all(values) else None


@dataclass(frozen=True)
class TrelloList:
    """A list the person can choose for their cards."""

    board: str
    id: str
    name: str


class TrelloConnector:
    """A person's own Trello boards, and the one list their cards go to.

    Trello's authorize page sends the token back in the URL fragment of ``return_url``,
    which carries our ``state``; the interface passes the token to ``finish`` as ``code``.
    """

    provider = "trello"
    title = "Trello"

    def __init__(self, settings: TrelloSettings, http: httpx.Client) -> None:
        """Bind the API key, the return URL and an HTTP client for Trello."""
        self._settings = settings
        self._http = http

    def _headers(self, token: str) -> dict[str, str]:
        # In a header, never the URL, so the token isn't in any request log (T10).
        key = self._settings.api_key
        return {
            "Accept": "application/json",
            "Authorization": f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"',
        }

    def _get(self, path: str, token: str, params: Mapping[str, str]) -> httpx.Response:
        try:
            return self._http.get(
                f"{TRELLO_API}{path}",
                params=params,
                headers=self._headers(token),
            )
        except httpx.HTTPError:
            raise _unreachable(self.title) from None

    def authorization_url(self, state: str, _code_challenge: str) -> str:
        """Return Trello's authorize URL for a read and write token that expires in 30 days.

        The token comes back in the return URL's fragment, so the browser's history keeps it
        (T10). Expiring it bounds how long a copy found there works; after that, Trello refuses
        it and the connection is marked ``needs_attention`` ("Reconnect Trello").
        """
        return_url = f"{self._settings.return_url}?{urlencode({'state': state})}"
        query = urlencode(
            {
                "key": self._settings.api_key,
                "name": APP_NAME,
                "scope": "read,write",
                "expiration": "30days",
                "response_type": "token",
                "callback_method": "fragment",
                "return_url": return_url,
            },
        )
        return f"{TRELLO_AUTHORIZE}?{query}"

    def finish(self, code: str, _code_verifier: str) -> Grant:
        """Check the token Trello returned by reading whose it is."""
        response = self._get("/members/me", code, {"fields": "username,fullName"})
        if response.status_code != httpx.codes.OK:
            raise _refused(self.title)
        member = response.json()
        name, username = str(member.get("fullName") or ""), str(member.get("username") or "")
        account = f"{name} (@{username})" if name and username else name or username
        return Grant(secret={"token": code, "list_id": ""}, account=account, scopes="read,write")

    def needs_setup(self, secret: Mapping[str, Any]) -> bool:
        """Whether the person still has to choose a list for their cards."""
        return not secret.get("list_id")

    def lists(self, secret: Mapping[str, Any]) -> list[TrelloList]:
        """Return the open lists on the person's open boards.

        Raises:
            ReconnectNeededError: If Trello no longer accepts the token.
            ConnectorError: If Trello can't be reached.

        """
        response = self._get(
            "/members/me/boards",
            str(secret["token"]),
            {"filter": "open", "fields": "name", "lists": "open"},
        )
        if response.status_code in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
            raise ReconnectNeededError
        if response.status_code != httpx.codes.OK:
            raise _unreachable(self.title)
        return [
            TrelloList(board=str(board.get("name", "")), id=str(item["id"]), name=str(item["name"]))
            for board in response.json()
            for item in board.get("lists", [])
        ]

    def choose_list(self, secret: dict[str, Any], list_id: str) -> dict[str, Any]:
        """Return the secret with ``list_id``, if it is one of the person's open lists.

        Raises:
            ConnectorError: 400 if the list isn't one of theirs.

        """
        if list_id not in {item.id for item in self.lists(secret)}:
            raise ConnectorError(400, "invalid_request", "That isn't one of your Trello lists")
        return {**secret, "list_id": list_id}

    def build(self, secret: dict[str, Any], _save: SaveSecret, _cleanup: ExitStack) -> object:
        """Return Team 4's provider for the person's token and list."""
        if self.needs_setup(secret):
            raise SetupNeededError
        from team_4_issue_tracker_trello import TrelloIssueProvider  # noqa: PLC0415

        issues: IssueProvider = TrelloIssueProvider(
            api_key=self._settings.api_key,
            token=str(secret["token"]),
            list_id=str(secret["list_id"]),
        )
        return issues

    def is_auth_failure(self, error: BaseException) -> bool:
        """Team 4 reports a refused token (401 or 403) as ``access_denied``."""
        return isinstance(error, ServiceError) and error.code == IssueErrorCode.ACCESS_DENIED

    def revoke(self, secret: Mapping[str, Any]) -> None:
        """Delete the token at Trello."""
        token = str(secret["token"])
        try:
            response = self._http.delete(
                f"{TRELLO_API}/tokens/{token}",
                headers=self._headers(token),
            )
        except httpx.HTTPError:
            raise _unreachable(self.title) from None
        # 401 means Trello no longer accepts the token: already revoked.
        if response.status_code not in {httpx.codes.OK, httpx.codes.UNAUTHORIZED}:
            raise _unreachable(self.title)


def build_connectors(http: httpx.Client) -> dict[str, Connector]:
    """Return a connector for each provider whose settings are present."""
    connectors: dict[str, Connector] = {}
    if (dropbox := dropbox_auth.connect_settings()) is not None:
        connectors["dropbox"] = DropboxConnector(dropbox)
    if (calendar := GoogleCalendarSettings.from_env()) is not None:
        connectors["google_calendar"] = GoogleCalendarConnector(calendar, http)
    if (trello := TrelloSettings.from_env()) is not None:
        connectors["trello"] = TrelloConnector(trello, http)
    return connectors


__all__ = [
    "Connector",
    "ConnectorError",
    "DropboxConnector",
    "GoogleCalendarConnector",
    "Grant",
    "ReconnectNeededError",
    "SetupNeededError",
    "TrelloConnector",
    "TrelloList",
    "build_connectors",
]
