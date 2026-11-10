"""Unit tests for the ``login`` / ``logout`` commands and the local OAuth redirect server.

The redirect-server tests start the real callback server on a free localhost port and talk
to it with ``requests``; nothing else touches the network. ``login`` runs the real OAuth
flow with ``_wait_for_redirect`` and the token exchange patched, and ``logout`` runs with
``dropbox.Dropbox`` patched with ``autospec=True`` so the revoke call is checked against
the real SDK. Every test runs with an empty environment and a token file in ``tmp_path``
(the ``isolated_env`` fixture), and output and logs are checked for leaked secrets.
"""

import logging
import os
import socket
import threading
import webbrowser
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from dropbox import oauth
from dropbox.exceptions import AuthError
from tests.auth_helpers import (
    APP_KEY,
    APP_SECRET,
    OAUTH_ENV,
    REDIRECT_URI,
    REFRESH_TOKEN,
    assert_no_secrets,
    flow_result,
    write_token_file,
)

from dropbox_client_impl import auth

pytestmark = pytest.mark.usefixtures("isolated_env")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


# ----- Local redirect server -----


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (REDIRECT_URI, ("localhost", 8080, "/oauth/callback")),
        ("http://127.0.0.1:9000", ("127.0.0.1", 9000, "/")),
    ],
)
def test_local_redirect_address(uri: str, expected: tuple[str, int, str]) -> None:
    """A localhost redirect URI is split into host, port, and path."""
    assert auth._local_redirect_address(uri) == expected


@pytest.mark.parametrize(
    "uri",
    ["https://localhost:8080/cb", "http://example.com:8080/cb", "http://localhost/cb"],
)
def test_local_redirect_address_rejects_non_local(uri: str) -> None:
    """Only local http URIs with an explicit port can be served by the login command."""
    with pytest.raises(auth.DropboxAuthError, match="DROPBOX_REDIRECT_URI"):
        auth._local_redirect_address(uri)


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


@patch("dropbox_client_impl.auth.check_auth", autospec=True, return_value=True)
@patch("dropbox_client_impl.auth._wait_for_redirect", autospec=True)
@patch.dict(os.environ, OAUTH_ENV)
def test_login_success_never_prints_tokens(
    mock_wait: MagicMock,
    mock_check: MagicMock,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Login opens the URL, saves credentials, verifies them, and never prints secrets."""
    caplog.set_level(logging.DEBUG)
    open_browser = Mock(spec=webbrowser.open)

    def fake_redirect(host: str, port: int, path: str) -> dict[str, str]:
        url = open_browser.call_args.args[0]
        state = parse_qs(urlsplit(url).query)["state"][0]
        return {"state": state, "code": "auth-code"}

    mock_wait.side_effect = fake_redirect

    with patch.object(oauth.DropboxOAuth2Flow, "_finish", return_value=flow_result()) as finish:
        assert auth.login(open_browser=open_browser) is True

    open_browser.assert_called_once()
    assert open_browser.call_args.args[0].startswith("https://www.dropbox.com/oauth2/authorize")
    mock_wait.assert_called_once_with("localhost", 8080, "/oauth/callback")
    finish.assert_called_once_with("auth-code", REDIRECT_URI, None)
    mock_check.assert_called_once_with()
    assert auth.load_refresh_token() == REFRESH_TOKEN
    out = capsys.readouterr().out
    assert "https://www.dropbox.com/oauth2/authorize" in out
    assert "Saved Dropbox credentials" in out
    assert_no_secrets(out, caplog.text)


def test_login_missing_config_fails(capsys: pytest.CaptureFixture[str]) -> None:
    """Login reports missing configuration and opens nothing."""
    open_browser = Mock(spec=webbrowser.open)

    assert auth.login(open_browser=open_browser) is False

    open_browser.assert_not_called()
    assert "DROPBOX_APP_KEY" in capsys.readouterr().out


@patch(
    "dropbox_client_impl.auth._wait_for_redirect",
    autospec=True,
    side_effect=OSError("Address already in use"),
)
@patch.dict(os.environ, OAUTH_ENV)
def test_login_port_in_use_fails(mock_wait: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    """A busy redirect port is reported instead of crashing."""
    assert auth.login(open_browser=Mock(spec=webbrowser.open)) is False

    mock_wait.assert_called_once_with("localhost", 8080, "/oauth/callback")
    assert "Address already in use" in capsys.readouterr().out


def test_logout_without_saved_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    """Logout is a no-op when nothing is saved."""
    assert auth.logout() is True
    assert "No saved Dropbox credentials" in capsys.readouterr().out


@pytest.mark.parametrize(
    "revoke_error",
    [None, AuthError("req", "invalid_access_token")],
    ids=["revoked", "revoke-fails"],
)
@patch("dropbox_client_impl.auth.dropbox.Dropbox", autospec=True)
@patch.dict(os.environ, OAUTH_ENV)
def test_logout_revokes_and_deletes(
    mock_dropbox: MagicMock,
    revoke_error: Exception | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Logout revokes the saved token and deletes the file, even if the revoke fails."""
    path = write_token_file()
    mock_dropbox.return_value.auth_token_revoke.side_effect = revoke_error

    assert auth.logout() is True

    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token=REFRESH_TOKEN,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )
    mock_dropbox.return_value.auth_token_revoke.assert_called_once_with()
    assert not path.exists()
    assert_no_secrets(capsys.readouterr().out)
