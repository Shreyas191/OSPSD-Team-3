"""Unit tests for Dropbox OAuth authentication and DropboxClient construction.

All HTTP and Dropbox SDK network calls are mocked (the redirect server test only talks to
localhost), so these tests need no Dropbox account or credentials.
"""

import json
import logging
import os
import socket
import stat
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from dropbox import oauth
from dropbox.exceptions import AuthError

from dropbox_client_impl import auth
from dropbox_client_impl.dropbox_impl import DropboxClient

APP_KEY = "test-app-key"
APP_SECRET = "test-app-secret-value"
REDIRECT_URI = "http://localhost:8080/oauth/callback"
REFRESH_TOKEN = "test-refresh-token-value"
ACCESS_TOKEN = "test-access-token-value"
OAUTH_ENV = {
    "DROPBOX_APP_KEY": APP_KEY,
    "DROPBOX_APP_SECRET": APP_SECRET,
    "DROPBOX_REDIRECT_URI": REDIRECT_URI,
}
SECRETS = (APP_SECRET, REFRESH_TOKEN, ACCESS_TOKEN)
HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409
PRIVATE_FILE_MODE = 0o600


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path) -> Any:
    """Clear DROPBOX_* variables and point the token file at a temp path for every test."""
    with patch.dict(os.environ, {"DROPBOX_TOKEN_FILE": str(tmp_path / "token.json")}, clear=True):
        yield


@pytest.fixture
def oauth_env() -> Any:
    """Configure the Dropbox app credentials."""
    with patch.dict(os.environ, OAUTH_ENV):
        yield


def _set_env(**values: str) -> None:
    os.environ.update(values)


def _write_token_file(token: str = REFRESH_TOKEN) -> Path:
    path = auth.get_token_file()
    path.write_text(json.dumps({"refresh_token": token, "account_id": "dbid:1"}))
    return path


def _response(status_code: int, json_body: Any = None, text: str = "") -> Mock:
    response = Mock(spec=requests.Response)
    response.status_code = status_code
    response.ok = status_code < HTTP_BAD_REQUEST
    response.reason = "Reason"
    if json_body is not None:
        response.json.return_value = json_body
        response.content = b"{...}"
        response.text = "{...}"
    else:
        response.json.side_effect = ValueError("no json")
        response.content = text.encode()
        response.text = text
    return response


def _flow_result(refresh_token: str | None = REFRESH_TOKEN) -> oauth.OAuth2FlowNoRedirectResult:
    return oauth.OAuth2FlowNoRedirectResult(
        ACCESS_TOKEN,
        "dbid:1",
        "uid",
        refresh_token,
        None,
        None,
    )


def _assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


# ----- Token file -----


def test_get_token_file_default() -> None:
    """Without DROPBOX_TOKEN_FILE the token file is .dropbox_token.json."""
    del os.environ["DROPBOX_TOKEN_FILE"]
    assert auth.get_token_file() == Path(".dropbox_token.json")


def test_save_refresh_token_writes_private_file() -> None:
    """Saved credentials are JSON and readable only by the owner."""
    path = auth.save_refresh_token(REFRESH_TOKEN, "dbid:1")

    assert json.loads(path.read_text()) == {"refresh_token": REFRESH_TOKEN, "account_id": "dbid:1"}
    assert stat.S_IMODE(path.stat().st_mode) == PRIVATE_FILE_MODE


def test_load_refresh_token_from_file() -> None:
    """The refresh token is read from the token file."""
    _write_token_file()
    assert auth.load_refresh_token() == REFRESH_TOKEN


def test_load_refresh_token_env_overrides_file() -> None:
    """DROPBOX_REFRESH_TOKEN takes precedence over the token file."""
    _write_token_file("file-token")
    _set_env(DROPBOX_REFRESH_TOKEN=REFRESH_TOKEN)
    assert auth.load_refresh_token() == REFRESH_TOKEN


def test_load_refresh_token_missing_returns_none() -> None:
    """No env var and no file means no refresh token."""
    assert auth.load_refresh_token() is None


@pytest.mark.parametrize("content", ["not json", "[]", '{"other": 1}'])
def test_load_refresh_token_corrupt_file_raises(content: str) -> None:
    """An unreadable token file raises a clear error telling the user to log in again."""
    auth.get_token_file().write_text(content)
    with pytest.raises(auth.DropboxAuthError, match="login"):
        auth.load_refresh_token()


# ----- get_dropbox_client -----


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
@pytest.mark.usefixtures("oauth_env")
def test_get_dropbox_client_uses_refresh_token(mock_dropbox: Any) -> None:
    """With a saved refresh token the SDK client refreshes access tokens itself."""
    _write_token_file()

    dbx = auth.get_dropbox_client()

    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token=REFRESH_TOKEN,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )
    assert dbx is mock_dropbox.return_value


@pytest.mark.parametrize("missing", ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"])
@patch("dropbox_client_impl.auth.dropbox.Dropbox")
def test_get_dropbox_client_refresh_token_without_app_config_raises(
    mock_dropbox: Any,
    missing: str,
) -> None:
    """A refresh token is useless without the app key and secret."""
    _write_token_file()
    _set_env(**{k: v for k, v in OAUTH_ENV.items() if k != missing})

    with pytest.raises(auth.DropboxAuthError, match=missing) as exc_info:
        auth.get_dropbox_client()

    _assert_no_secrets(str(exc_info.value))
    mock_dropbox.assert_not_called()


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
def test_get_dropbox_client_access_token_fallback(mock_dropbox: Any) -> None:
    """DROPBOX_ACCESS_TOKEN is used only when no refresh token is available."""
    _set_env(DROPBOX_ACCESS_TOKEN=ACCESS_TOKEN)

    auth.get_dropbox_client()

    mock_dropbox.assert_called_once_with(
        oauth2_access_token=ACCESS_TOKEN,
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
@pytest.mark.usefixtures("oauth_env")
def test_get_dropbox_client_prefers_refresh_token(mock_dropbox: Any) -> None:
    """OAuth credentials win over the manual access-token fallback."""
    _write_token_file()
    _set_env(DROPBOX_ACCESS_TOKEN=ACCESS_TOKEN)

    auth.get_dropbox_client()

    assert mock_dropbox.call_args.kwargs["oauth2_refresh_token"] == REFRESH_TOKEN


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
@pytest.mark.usefixtures("oauth_env")
def test_get_dropbox_client_not_authorized_raises(mock_dropbox: Any) -> None:
    """Without any credentials the error tells the user to run the login command."""
    with pytest.raises(auth.DropboxAuthError, match="login"):
        auth.get_dropbox_client()
    mock_dropbox.assert_not_called()


# ----- get_access_token / headers -----


def test_get_access_token_with_fallback_token() -> None:
    """A manual access token is returned as-is (no refresh possible or needed)."""
    _set_env(DROPBOX_ACCESS_TOKEN=ACCESS_TOKEN)
    assert auth.get_access_token() == ACCESS_TOKEN


@patch("dropbox_client_impl.auth.get_dropbox_client")
def test_get_access_token_refreshes(mock_client: Any) -> None:
    """The SDK refreshes the short-lived access token before it is returned."""
    dbx = mock_client.return_value
    dbx._oauth2_access_token = ACCESS_TOKEN

    assert auth.get_access_token() == ACCESS_TOKEN
    dbx.check_and_refresh_access_token.assert_called_once_with()


@patch("dropbox_client_impl.auth.get_dropbox_client")
def test_get_access_token_revoked_refresh_token_raises(mock_client: Any) -> None:
    """A revoked/expired refresh token becomes a DropboxAuthError without leaking it."""
    mock_client.return_value.check_and_refresh_access_token.side_effect = AuthError(
        "req",
        "invalid_access_token",
    )

    with pytest.raises(auth.DropboxAuthError, match="revoked or expired") as exc_info:
        auth.get_access_token()

    _assert_no_secrets(str(exc_info.value))


@patch("dropbox_client_impl.auth.get_dropbox_client")
def test_get_access_token_network_error(mock_client: Any) -> None:
    """Network failures while refreshing raise DropboxConnectionError."""
    mock_client.return_value.check_and_refresh_access_token.side_effect = requests.ConnectionError()

    with pytest.raises(auth.DropboxConnectionError):
        auth.get_access_token()


def test_get_auth_headers() -> None:
    """The Authorization header uses the Bearer scheme."""
    _set_env(DROPBOX_ACCESS_TOKEN=ACCESS_TOKEN)
    assert auth.get_auth_headers() == {"Authorization": f"Bearer {ACCESS_TOKEN}"}


# ----- dropbox_request -----


@pytest.fixture
def access_token_env() -> Any:
    """Use the manual access-token fallback so requests need no refresh call."""
    with patch.dict(os.environ, {"DROPBOX_ACCESS_TOKEN": ACCESS_TOKEN}):
        yield


@patch("dropbox_client_impl.auth.requests.post")
@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_success(mock_post: Any) -> None:
    """A successful call posts to the right URL with auth and returns JSON."""
    mock_post.return_value = _response(200, {"ok": True})

    result = auth.dropbox_request("/files/get_metadata", {"path": "/a"})

    assert result == {"ok": True}
    mock_post.assert_called_once_with(
        "https://api.dropboxapi.com/2/files/get_metadata",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        json={"path": "/a"},
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )


@patch("dropbox_client_impl.auth.requests.post")
@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_empty_body_returns_none(mock_post: Any) -> None:
    """An empty response body returns None."""
    mock_post.return_value = _response(200, text="")

    assert auth.dropbox_request("users/get_space_usage") is None


@patch("dropbox_client_impl.auth.requests.post")
@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_invalid_token_raises_auth_error(mock_post: Any) -> None:
    """HTTP 401 (invalid/expired token) raises DropboxAuthError pointing to login."""
    mock_post.return_value = _response(401, {"error_summary": "expired_access_token/"})

    with pytest.raises(auth.DropboxAuthError, match="expired_access_token") as exc_info:
        auth.dropbox_request("/users/get_current_account")

    assert "login" in str(exc_info.value)
    _assert_no_secrets(str(exc_info.value))


@patch("dropbox_client_impl.auth.requests.post")
@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_api_error_json(mock_post: Any) -> None:
    """Other error responses raise DropboxAPIError with the status code."""
    mock_post.return_value = _response(HTTP_CONFLICT, {"error_summary": "path/not_found/"})

    with pytest.raises(auth.DropboxAPIError, match="path/not_found") as exc_info:
        auth.dropbox_request("/files/get_metadata", {"path": "/missing"})

    assert exc_info.value.status_code == HTTP_CONFLICT


@pytest.mark.parametrize(
    ("json_body", "text", "expected"),
    [
        (None, "Error in call to API function: bad input", "bad input"),
        (None, "", "Reason"),
        ({"error": "boom"}, "", "boom"),
        (["unexpected"], "", "unexpected"),
    ],
)
@patch("dropbox_client_impl.auth.requests.post")
@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_api_error_message_formats(
    mock_post: Any,
    json_body: Any,
    text: str,
    expected: str,
) -> None:
    """Error messages are extracted from JSON or plain-text bodies."""
    mock_post.return_value = _response(400, json_body, text)

    with pytest.raises(auth.DropboxAPIError, match=expected):
        auth.dropbox_request("/x")


@patch(
    "dropbox_client_impl.auth.requests.post",
    side_effect=requests.ConnectionError("dns failure"),
)
@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_network_error(mock_post: Any) -> None:
    """Network failures raise DropboxConnectionError."""
    with pytest.raises(auth.DropboxConnectionError, match="dns failure"):
        auth.dropbox_request("/users/get_current_account")


@patch("dropbox_client_impl.auth.requests.post")
def test_dropbox_request_without_credentials_makes_no_request(mock_post: Any) -> None:
    """No HTTP request is made without credentials."""
    with pytest.raises(auth.DropboxAuthError):
        auth.dropbox_request("/users/get_current_account")

    mock_post.assert_not_called()


# ----- get_current_account / check_auth -----


@patch("dropbox_client_impl.auth.dropbox_request")
def test_get_current_account(mock_request: Any) -> None:
    """get_current_account calls users/get_current_account."""
    mock_request.return_value = {"account_id": "dbid:1"}

    assert auth.get_current_account() == {"account_id": "dbid:1"}
    mock_request.assert_called_once_with("/users/get_current_account")


@patch("dropbox_client_impl.auth.get_current_account")
def test_check_auth_success(mock_account: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """check_auth reports success with the account name."""
    mock_account.return_value = {"name": {"display_name": "Ada"}, "email": "ada@example.com"}

    assert auth.check_auth() is True
    assert "authentication OK: Ada <ada@example.com>" in capsys.readouterr().out


@patch(
    "dropbox_client_impl.auth.get_current_account",
    side_effect=auth.DropboxAuthError("bad token"),
)
def test_check_auth_failure(mock_account: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """check_auth reports the error and returns False."""
    assert auth.check_auth() is False
    assert "authentication FAILED: bad token" in capsys.readouterr().out


@patch("dropbox_client_impl.auth.requests.post")
@patch("dropbox_client_impl.auth.get_dropbox_client")
def test_check_auth_with_refresh_token_end_to_end(
    mock_client: Any,
    mock_post: Any,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """check_auth refreshes, calls the API with the fresh token, and never prints it."""
    caplog.set_level(logging.DEBUG)
    mock_client.return_value._oauth2_access_token = ACCESS_TOKEN
    mock_post.return_value = _response(
        200,
        {"name": {"display_name": "Ada"}, "email": "ada@example.com"},
    )

    assert auth.check_auth() is True
    assert mock_post.call_args.kwargs["headers"] == {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    _assert_no_secrets(capsys.readouterr().out, caplog.text)


# ----- OAuth authorization URL and configuration -----


@pytest.mark.usefixtures("oauth_env")
def test_authorization_url() -> None:
    """The authorization URL requests an offline (refresh) token for our app and redirect URI."""
    session: dict[str, str] = {}
    url = auth.get_authorization_flow(session).start()

    parsed = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert parsed.netloc == "www.dropbox.com"
    assert parsed.path == "/oauth2/authorize"
    assert query["client_id"] == APP_KEY
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["response_type"] == "code"
    assert query["token_access_type"] == "offline"
    assert query["state"] == session[auth.CSRF_SESSION_KEY]
    assert APP_SECRET not in url


@pytest.mark.parametrize("missing", list(OAUTH_ENV))
def test_authorization_flow_missing_config_raises(missing: str) -> None:
    """Each missing OAuth variable is named in the error."""
    _set_env(**{k: v for k, v in OAUTH_ENV.items() if k != missing})

    with pytest.raises(auth.DropboxAuthError, match=missing) as exc_info:
        auth.get_authorization_flow({})

    _assert_no_secrets(str(exc_info.value))


def test_authorization_flow_blank_config_raises() -> None:
    """Whitespace-only values count as missing."""
    _set_env(**{**OAUTH_ENV, "DROPBOX_APP_SECRET": "   "})
    with pytest.raises(auth.DropboxAuthError, match="DROPBOX_APP_SECRET"):
        auth.get_authorization_flow({})


# ----- finish_authorization -----


def _started_flow() -> tuple[oauth.DropboxOAuth2Flow, str]:
    session: dict[str, str] = {}
    flow = auth.get_authorization_flow(session)
    flow.start()
    return flow, session[auth.CSRF_SESSION_KEY]


@pytest.mark.usefixtures("oauth_env")
def test_finish_authorization_saves_refresh_token() -> None:
    """A valid redirect exchanges the code and stores only the refresh token locally."""
    flow, state = _started_flow()

    with patch.object(flow, "_finish", return_value=_flow_result()) as mock_finish:
        account_id = auth.finish_authorization(flow, {"state": state, "code": "auth-code"})

    assert account_id == "dbid:1"
    mock_finish.assert_called_once_with("auth-code", REDIRECT_URI, None)
    saved = json.loads(auth.get_token_file().read_text())
    assert saved == {"refresh_token": REFRESH_TOKEN, "account_id": "dbid:1"}
    assert auth.load_refresh_token() == REFRESH_TOKEN


@pytest.mark.usefixtures("oauth_env")
def test_finish_authorization_denied() -> None:
    """Clicking "Cancel" in Dropbox raises a clear error and saves nothing."""
    flow, state = _started_flow()

    with pytest.raises(auth.DropboxAuthError, match="denied"):
        auth.finish_authorization(flow, {"state": state, "error": "access_denied"})

    assert not auth.get_token_file().exists()


@pytest.mark.parametrize(
    "params",
    [
        {"state": "forged-state-value-1234567890", "code": "auth-code"},
        {"code": "auth-code"},
        {"state": "STATE", "error": "server_error", "error_description": "oops"},
    ],
)
@pytest.mark.usefixtures("oauth_env")
def test_finish_authorization_invalid_redirect(params: dict[str, str]) -> None:
    """CSRF mismatches, malformed redirects, and provider errors are rejected."""
    flow, state = _started_flow()
    params = {k: state if v == "STATE" else v for k, v in params.items()}

    with pytest.raises(auth.DropboxAuthError, match="authorization failed"):
        auth.finish_authorization(flow, params)

    assert not auth.get_token_file().exists()


@pytest.mark.usefixtures("oauth_env")
def test_finish_authorization_rejected_code() -> None:
    """An expired/invalid authorization code is reported without leaking the secret."""
    flow, state = _started_flow()

    with (
        patch.object(flow, "_finish", side_effect=requests.HTTPError("400 Client Error")),
        pytest.raises(auth.DropboxAuthError, match="HTTPError") as exc_info,
    ):
        auth.finish_authorization(flow, {"state": state, "code": "expired-code"})

    _assert_no_secrets(str(exc_info.value))


@pytest.mark.usefixtures("oauth_env")
def test_finish_authorization_without_refresh_token() -> None:
    """A response without a refresh token is an error (we would be logged out in hours)."""
    flow, state = _started_flow()

    with (
        patch.object(flow, "_finish", return_value=_flow_result(refresh_token=None)),
        pytest.raises(auth.DropboxAuthError, match="refresh token"),
    ):
        auth.finish_authorization(flow, {"state": state, "code": "auth-code"})


# ----- Local redirect server -----


def test_local_redirect_address() -> None:
    """A localhost redirect URI is split into host, port, and path."""
    assert auth._local_redirect_address(REDIRECT_URI) == ("localhost", 8080, "/oauth/callback")
    assert auth._local_redirect_address("http://127.0.0.1:9000") == ("127.0.0.1", 9000, "/")


@pytest.mark.parametrize(
    "uri",
    ["https://localhost:8080/cb", "http://example.com:8080/cb", "http://localhost/cb"],
)
def test_local_redirect_address_rejects_non_local(uri: str) -> None:
    """Only local http URIs with an explicit port can be served by the login command."""
    with pytest.raises(auth.DropboxAuthError, match="DROPBOX_REDIRECT_URI"):
        auth._local_redirect_address(uri)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_wait_for_redirect_captures_query_params(capsys: pytest.CaptureFixture[str]) -> None:
    """The callback server returns the redirect's query parameters and logs nothing."""
    port = _free_port()
    result: dict[str, dict[str, str]] = {}
    thread = threading.Thread(
        target=lambda: result.update(
            params=auth._wait_for_redirect("127.0.0.1", port, "/cb", timeout=10),
        ),
    )
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            not_found = requests.get(f"{base}/favicon.ico", timeout=5)
            break
        except requests.ConnectionError:
            threading.Event().wait(0.05)
    ok = requests.get(f"{base}/cb?code=secret-code&state=abc", timeout=5)
    thread.join(timeout=10)

    assert not_found.status_code == auth.HTTP_NOT_FOUND
    assert ok.status_code == auth.HTTP_OK
    assert "close this tab" in ok.text
    assert result["params"] == {"code": "secret-code", "state": "abc"}
    captured = capsys.readouterr()
    assert "secret-code" not in captured.out + captured.err


def test_wait_for_redirect_times_out() -> None:
    """If the browser never comes back, login fails instead of hanging."""
    with pytest.raises(auth.DropboxAuthError, match="Timed out"):
        auth._wait_for_redirect("127.0.0.1", _free_port(), "/cb", timeout=0.1)


# ----- login / logout -----


@patch("dropbox_client_impl.auth.check_auth", return_value=True)
@patch("dropbox_client_impl.auth._wait_for_redirect")
@pytest.mark.usefixtures("oauth_env")
def test_login_success_never_prints_tokens(
    mock_wait: Any,
    mock_check: Any,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Login opens the URL, saves credentials, verifies them, and never prints secrets."""
    caplog.set_level(logging.DEBUG)
    open_browser = Mock()

    def fake_redirect(host: str, port: int, path: str) -> dict[str, str]:
        assert (host, port, path) == ("localhost", 8080, "/oauth/callback")
        url = open_browser.call_args.args[0]
        state = parse_qs(urlsplit(url).query)["state"][0]
        return {"state": state, "code": "auth-code"}

    mock_wait.side_effect = fake_redirect

    with patch.object(oauth.DropboxOAuth2Flow, "_finish", return_value=_flow_result()):
        assert auth.login(open_browser=open_browser) is True

    open_browser.assert_called_once()
    mock_check.assert_called_once_with()
    assert auth.load_refresh_token() == REFRESH_TOKEN
    out = capsys.readouterr().out
    assert "https://www.dropbox.com/oauth2/authorize" in out
    assert "Saved Dropbox credentials" in out
    _assert_no_secrets(out, caplog.text)


def test_login_missing_config_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """Login reports missing configuration and opens nothing."""
    open_browser = Mock()

    assert auth.login(open_browser=open_browser) is False

    open_browser.assert_not_called()
    assert "DROPBOX_APP_KEY" in capsys.readouterr().out


@patch("dropbox_client_impl.auth._wait_for_redirect", side_effect=OSError("Address already in use"))
@pytest.mark.usefixtures("oauth_env")
def test_login_port_in_use_fails(mock_wait: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """A busy redirect port is reported instead of crashing."""
    assert auth.login(open_browser=Mock()) is False
    assert "Address already in use" in capsys.readouterr().out


def test_logout_without_saved_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    """Logout is a no-op when nothing is saved."""
    assert auth.logout() is True
    assert "No saved Dropbox credentials" in capsys.readouterr().out


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
@pytest.mark.usefixtures("oauth_env")
def test_logout_revokes_and_deletes(mock_dropbox: Any) -> None:
    """Logout revokes the saved token with Dropbox and deletes the file."""
    path = _write_token_file()

    assert auth.logout() is True

    assert mock_dropbox.call_args.kwargs["oauth2_refresh_token"] == REFRESH_TOKEN
    mock_dropbox.return_value.auth_token_revoke.assert_called_once_with()
    assert not path.exists()


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
@pytest.mark.usefixtures("oauth_env")
def test_logout_deletes_even_if_revoke_fails(
    mock_dropbox: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed revoke (e.g. already revoked) still removes the local file."""
    path = _write_token_file()
    mock_dropbox.return_value.auth_token_revoke.side_effect = AuthError(
        "req",
        "invalid_access_token",
    )

    assert auth.logout() is True

    assert not path.exists()
    _assert_no_secrets(capsys.readouterr().out)


# ----- DropboxClient -----


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
def test_init_with_provided_instance_skips_auth(mock_dropbox: Any) -> None:
    """Passing a Dropbox instance uses it directly and skips auth."""
    dbx = Mock()

    client = DropboxClient(dbx=dbx)

    assert client.dbx is dbx
    mock_dropbox.assert_not_called()


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
@pytest.mark.usefixtures("oauth_env")
def test_init_uses_get_dropbox_client(mock_dropbox: Any) -> None:
    """DropboxClient gets its OAuth-backed SDK instance from get_dropbox_client."""
    _write_token_file()

    client = DropboxClient()

    assert client.dbx is mock_dropbox.return_value
    assert mock_dropbox.call_args.kwargs["oauth2_refresh_token"] == REFRESH_TOKEN


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
def test_init_not_authorized_raises(mock_dropbox: Any) -> None:
    """DropboxClient fails clearly when the user has not logged in."""
    with pytest.raises(auth.DropboxAuthError, match="login"):
        DropboxClient()

    mock_dropbox.assert_not_called()
