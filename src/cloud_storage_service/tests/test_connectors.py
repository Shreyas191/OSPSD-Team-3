"""Each provider's connector on its own (#99, #100, #101), with the provider's HTTP faked.

Google and Trello are ``httpx.MockTransport`` handlers; Dropbox's connector is checked
against ``dropbox_client_impl.connect``, which has its own tests with the SDK mocked.
"""

import json
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from calendar_contract import CalendarError, ErrorCode
from dropbox_client_impl import DropboxAuthError, DropboxConnectionError, DropboxDocumentStorage
from dropbox_client_impl.auth import ConnectSettings
from dropbox_client_impl.connect import DropboxGrant
from google.oauth2.credentials import Credentials
from issue_tracker_contract.errors import ErrorCode as IssueErrorCode
from issue_tracker_contract.errors import ServiceError

from cloud_storage_service.connectors import (
    CALENDAR_REDIRECT_ENV_VAR,
    CALENDAR_SCOPE,
    GOOGLE_REVOKE_ENDPOINT,
    TRELLO_KEY_ENV_VAR,
    TRELLO_RETURN_ENV_VAR,
    ConnectorError,
    DropboxConnector,
    GoogleCalendarConnector,
    GoogleCalendarSettings,
    ReconnectNeededError,
    SetupNeededError,
    TrelloConnector,
    TrelloSettings,
    build_connectors,
)
from cloud_storage_service.sign_in import CLIENT_ID_ENV_VAR, CLIENT_SECRET_ENV_VAR, TOKEN_ENDPOINT

CALENDAR = GoogleCalendarSettings("client-123", "secret-xyz", "https://web.test/calendar")
Handler = Callable[[httpx.Request], httpx.Response]


def http(handler: Handler) -> httpx.Client:
    """Return an HTTP client whose every request goes to ``handler``."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def form(request: httpx.Request) -> dict[str, str]:
    """Return a form-encoded request body as a dict."""
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def query(url: str) -> dict[str, str]:
    """Return a URL's query parameters as a dict."""
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def unreachable(request: httpx.Request) -> httpx.Response:
    """Fail as an unreachable host does."""
    msg = "no route"
    raise httpx.ConnectError(msg, request=request)


def no_save(_secret: dict[str, Any]) -> None:
    """Fail the test if the connector saves a secret."""
    pytest.fail("nothing should be saved")


def email_claims(token: str, audience: str) -> dict[str, Any]:
    """Stand in for Google's ID token check: the fake's token is its claims as JSON."""
    assert audience == CALENDAR.client_id
    claims: dict[str, Any] = json.loads(token)
    return claims


# ----- Google Calendar -----


def test_the_calendar_consent_asks_only_for_events_offline_with_pkce() -> None:
    """The URL asks for ``calendar.events`` (plus who the account is), offline, S256."""
    connector = GoogleCalendarConnector(CALENDAR, http(unreachable))

    params = query(connector.authorization_url("state-1", "challenge-1"))

    assert params["scope"] == f"openid email {CALENDAR_SCOPE}"
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["redirect_uri"] == CALENDAR.redirect_uri
    assert params["state"] == "state-1"
    assert (params["code_challenge"], params["code_challenge_method"]) == ("challenge-1", "S256")
    assert CALENDAR.client_secret not in connector.authorization_url("s", "c")


def calendar_grant(body: dict[str, Any], status: int = 200) -> Handler:
    """Answer a code exchange with ``body``, after checking what was sent."""

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_ENDPOINT
        sent = form(request)
        assert sent["grant_type"] == "authorization_code"
        assert (sent["code"], sent["code_verifier"]) == ("code-1", "verifier-1")
        return httpx.Response(status, json=body)

    return handle


def test_a_calendar_grant_keeps_the_refresh_token_and_shows_the_account() -> None:
    """The secret holds the refresh and access tokens; the account is the ID token's email."""
    body = {
        "refresh_token": "refresh-1",
        "access_token": "access-1",
        "expires_in": 3600,
        "scope": f"openid {CALENDAR_SCOPE} email",
        "id_token": json.dumps({"email": "alice@example.test"}),
    }
    connector = GoogleCalendarConnector(CALENDAR, http(calendar_grant(body)), email_claims)

    grant = connector.finish("code-1", "verifier-1")

    assert grant.account == "alice@example.test"
    assert grant.secret["refresh_token"] == "refresh-1"
    assert grant.secret["access_token"] == "access-1"
    assert datetime.fromisoformat(grant.secret["expiry"]) > datetime.now(UTC)
    assert not connector.needs_setup(grant.secret)


def test_a_calendar_grant_without_a_readable_id_token_still_connects() -> None:
    """The email is only for display; a bad ID token leaves it empty."""

    def bad(_token: str, _audience: str) -> dict[str, Any]:
        msg = "bad"
        raise ValueError(msg)

    body = {"refresh_token": "r", "scope": CALENDAR_SCOPE, "id_token": "x"}
    connector = GoogleCalendarConnector(CALENDAR, http(calendar_grant(body)), bad)

    assert connector.finish("code-1", "verifier-1").account == ""


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"access_token": "a", "scope": CALENDAR_SCOPE}, 200),  # no refresh token
        ({"refresh_token": "r", "scope": "openid email"}, 200),  # calendar access unticked
        ({"error": "invalid_grant"}, 400),
    ],
)
def test_a_calendar_grant_without_calendar_access_is_refused(
    body: dict[str, Any],
    status: int,
) -> None:
    """No refresh token, no ``calendar.events``, or a refused code → 400."""
    connector = GoogleCalendarConnector(CALENDAR, http(calendar_grant(body, status)))

    with pytest.raises(ConnectorError) as raised:
        connector.finish("code-1", "verifier-1")

    assert (raised.value.status, raised.value.code) == (400, "connect_failed")


def test_google_unreachable_is_503() -> None:
    """A network failure during the exchange is 503."""
    connector = GoogleCalendarConnector(CALENDAR, http(unreachable))

    with pytest.raises(ConnectorError) as raised:
        connector.finish("code-1", "verifier-1")

    assert raised.value.status == 503


def secret(expires_in: timedelta) -> dict[str, Any]:
    """Return a calendar secret whose access token expires after ``expires_in``."""
    return {
        "refresh_token": "refresh-1",
        "access_token": "access-1",
        "expiry": (datetime.now(UTC) + expires_in).isoformat(),
    }


def test_the_token_file_is_one_team5_reads_and_is_gone_after_the_request() -> None:
    """Team 5 gets a valid ``calendar.events`` token file; closing the request removes it."""
    connector = GoogleCalendarConnector(CALENDAR, http(unreachable))
    cleanup = ExitStack()

    with patch("team5_calendar.create_calendar_service") as create:
        connector.build(secret(timedelta(hours=1)), no_save, cleanup)
        path = Path(create.call_args.kwargs["token_file"])
        credentials = Credentials.from_authorized_user_file(str(path))  # type: ignore[no-untyped-call]
        mode = path.stat().st_mode & 0o777
        cleanup.close()

    assert credentials.valid
    assert credentials.has_scopes([CALENDAR_SCOPE])  # type: ignore[no-untyped-call]
    assert credentials.refresh_token == "refresh-1"
    assert mode == 0o600
    assert not path.exists()


def test_a_nearly_expired_token_is_refreshed_and_saved_back() -> None:
    """Under five minutes left → refreshed before Team 5 sees it, and saved."""
    saved: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent = form(request)
        assert (sent["grant_type"], sent["refresh_token"]) == ("refresh_token", "refresh-1")
        return httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600})

    connector = GoogleCalendarConnector(CALENDAR, http(handle))

    with ExitStack() as cleanup, patch("team5_calendar.create_calendar_service") as create:
        connector.build(secret(timedelta(minutes=2)), saved.append, cleanup)
        token = json.loads(Path(create.call_args.kwargs["token_file"]).read_text())["token"]

    assert token == "access-2"
    assert saved[0]["access_token"] == "access-2"
    assert saved[0]["refresh_token"] == "refresh-1"


@pytest.mark.parametrize(
    ("status", "error"),
    [(400, ReconnectNeededError), (401, ReconnectNeededError), (500, ConnectorError)],
)
def test_a_refused_refresh_needs_a_reconnect(status: int, error: type[Exception]) -> None:
    """``invalid_grant`` (revoked, or Testing mode's 7 days) → reconnect; 5xx → unavailable."""
    connector = GoogleCalendarConnector(
        CALENDAR,
        http(lambda _r: httpx.Response(status, json={"error": "invalid_grant"})),
    )

    with ExitStack() as cleanup, pytest.raises(error):
        connector.build(secret(-timedelta(minutes=1)), no_save, cleanup)


def test_team5s_unavailable_authorization_is_an_auth_failure() -> None:
    """Non-retryable ``provider_unavailable`` means the token; a retryable one doesn't."""
    connector = GoogleCalendarConnector(CALENDAR, http(unreachable))

    assert connector.is_auth_failure(
        CalendarError(ErrorCode.PROVIDER_UNAVAILABLE, "x", retryable=False),
    )
    assert not connector.is_auth_failure(
        CalendarError(ErrorCode.PROVIDER_UNAVAILABLE, "x", retryable=True),
    )
    assert not connector.is_auth_failure(CalendarError(ErrorCode.NOT_FOUND, "x", retryable=False))
    assert not connector.is_auth_failure(ValueError("x"))


@pytest.mark.parametrize(("status", "ok"), [(200, True), (400, True), (500, False)])
def test_calendar_revoke(status: int, ok: bool) -> None:  # noqa: FBT001
    """Revoke posts the refresh token to Google; 400 (already gone) counts as done."""
    sent: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GOOGLE_REVOKE_ENDPOINT
        sent.append(form(request))
        return httpx.Response(status)

    connector = GoogleCalendarConnector(CALENDAR, http(handle))

    if ok:
        connector.revoke({"refresh_token": "refresh-1"})
    else:
        with pytest.raises(ConnectorError):
            connector.revoke({"refresh_token": "refresh-1"})
    assert sent == [{"token": "refresh-1"}]


def test_calendar_revoke_unreachable() -> None:
    """A network failure during revoke is 503."""
    with pytest.raises(ConnectorError):
        GoogleCalendarConnector(CALENDAR, http(unreachable)).revoke({"refresh_token": "r"})


# ----- Trello -----

TRELLO = TrelloSettings("key-123", "https://web.test/connections/trello")


def test_the_trello_authorize_url_returns_to_us_with_the_state() -> None:
    """Read and write, expiring in 30 days, token in the fragment of our return URL."""
    connector = TrelloConnector(TRELLO, http(unreachable))

    params = query(connector.authorization_url("state-1", "ignored"))

    assert params["key"] == TRELLO.api_key
    assert params["scope"] == "read,write"
    assert params["expiration"] == "30days"
    assert (params["response_type"], params["callback_method"]) == ("token", "fragment")
    assert query(params["return_url"]) == {"state": "state-1"}
    assert params["return_url"].startswith(TRELLO.return_url)


def test_trello_build_passes_every_argument_explicitly() -> None:
    """Team 4's provider gets the person's token and list, never the environment's."""
    connector = TrelloConnector(TRELLO, http(unreachable))

    with ExitStack() as cleanup:
        provider = connector.build({"token": "t-1", "list_id": "l-1"}, no_save, cleanup)
        with pytest.raises(SetupNeededError):
            connector.build({"token": "t-1", "list_id": ""}, no_save, cleanup)

    assert (provider._api_key, provider._token, provider._list_id) == ("key-123", "t-1", "l-1")  # type: ignore[attr-defined]


def test_trello_access_denied_is_an_auth_failure() -> None:
    """Team 4's ``access_denied`` (401, 403) means the token."""
    connector = TrelloConnector(TRELLO, http(unreachable))

    assert connector.is_auth_failure(ServiceError(IssueErrorCode.ACCESS_DENIED, "x"))
    assert not connector.is_auth_failure(ServiceError(IssueErrorCode.NOT_FOUND, "x"))


@pytest.mark.parametrize(("status", "ok"), [(200, True), (401, True), (500, False)])
def test_trello_revoke(status: int, ok: bool) -> None:  # noqa: FBT001
    """Revoke deletes the token at Trello; 401 (already gone) counts as done."""
    connector = TrelloConnector(TRELLO, http(lambda _r: httpx.Response(status)))

    if ok:
        connector.revoke({"token": "t-1"})
    else:
        with pytest.raises(ConnectorError):
            connector.revoke({"token": "t-1"})


@pytest.mark.parametrize("call", ["finish", "lists", "revoke"])
def test_trello_unreachable_is_503(call: str) -> None:
    """Every Trello call reports a network failure as 503."""
    connector = TrelloConnector(TRELLO, http(unreachable))
    calls: dict[str, Callable[[], object]] = {
        "finish": lambda: connector.finish("t-1", ""),
        "lists": lambda: connector.lists({"token": "t-1"}),
        "revoke": lambda: connector.revoke({"token": "t-1"}),
    }

    with pytest.raises(ConnectorError) as raised:
        calls[call]()

    assert raised.value.status == 503


def test_trello_lists_on_a_server_error_are_unavailable() -> None:
    """A 5xx listing boards is 503, not a reconnect."""
    connector = TrelloConnector(TRELLO, http(lambda _r: httpx.Response(500)))

    with pytest.raises(ConnectorError):
        connector.lists({"token": "t-1"})


# ----- Dropbox -----

DROPBOX = ConnectSettings("app-key", "app-secret", "https://web.test/connections/dropbox")


def test_dropbox_finish_keeps_the_refresh_token_and_shows_the_email() -> None:
    """The secret is the refresh token; the account is the Dropbox email."""
    connector = DropboxConnector(DROPBOX)
    grant = DropboxGrant("refresh-1", "dbid:1", "alice@example.test", "files.content.write")

    with patch("dropbox_client_impl.connect.exchange_code", return_value=grant) as exchange:
        result = connector.finish("code-1", "verifier-1")

    exchange.assert_called_once_with(DROPBOX, "code-1", "verifier-1")
    assert result.secret == {"refresh_token": "refresh-1", "account_id": "dbid:1"}
    assert result.account == "alice@example.test"
    assert "state=s" in connector.authorization_url("s", "c")
    assert not connector.needs_setup(result.secret)


@pytest.mark.parametrize(
    ("error", "status"),
    [(DropboxAuthError("no"), 400), (DropboxConnectionError("down"), 503)],
)
def test_dropbox_finish_failures(error: Exception, status: int) -> None:
    """Dropbox refusing is 400; Dropbox unreachable is 503."""
    with (
        patch("dropbox_client_impl.connect.exchange_code", side_effect=error),
        pytest.raises(ConnectorError) as raised,
    ):
        DropboxConnector(DROPBOX).finish("code-1", "verifier-1")

    assert raised.value.status == status


def test_dropbox_build_and_revoke_use_the_refresh_token() -> None:
    """Storage acts as the person; revoke ends their token; a failed revoke is 503."""
    connector = DropboxConnector(DROPBOX)

    with ExitStack() as cleanup:
        storage = connector.build({"refresh_token": "refresh-1"}, no_save, cleanup)
    with patch("dropbox_client_impl.connect.revoke") as revoke:
        connector.revoke({"refresh_token": "refresh-1"})
    with (
        patch("dropbox_client_impl.connect.revoke", side_effect=DropboxConnectionError("x")),
        pytest.raises(ConnectorError),
    ):
        connector.revoke({"refresh_token": "refresh-1"})

    assert isinstance(storage, DropboxDocumentStorage)
    revoke.assert_called_once_with(DROPBOX, "refresh-1")
    assert not connector.is_auth_failure(ValueError("x"))


# ----- Which connectors are on -----


def test_each_connector_is_on_only_with_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No settings → none; each provider's settings → that connector."""
    for name in (
        "DROPBOX_APP_KEY",
        "DROPBOX_APP_SECRET",
        "DROPBOX_CONNECT_REDIRECT_URI",
        CLIENT_ID_ENV_VAR,
        CLIENT_SECRET_ENV_VAR,
        CALENDAR_REDIRECT_ENV_VAR,
        TRELLO_KEY_ENV_VAR,
        TRELLO_RETURN_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    client = http(unreachable)
    assert build_connectors(client) == {}

    for name, value in {
        "DROPBOX_APP_KEY": "k",
        "DROPBOX_APP_SECRET": "s",
        "DROPBOX_CONNECT_REDIRECT_URI": "https://web.test/d",
        CLIENT_ID_ENV_VAR: "c",
        CLIENT_SECRET_ENV_VAR: "s",
        CALENDAR_REDIRECT_ENV_VAR: "https://web.test/c",
        TRELLO_KEY_ENV_VAR: "k",
        TRELLO_RETURN_ENV_VAR: "https://web.test/t",
    }.items():
        monkeypatch.setenv(name, value)

    assert sorted(build_connectors(client)) == ["dropbox", "google_calendar", "trello"]
