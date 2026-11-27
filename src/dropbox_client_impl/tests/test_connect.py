"""Unit tests for connecting a person's own Dropbox (#99), with HTTP and the SDK mocked."""

import os
from collections.abc import Mapping
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import dropbox
import pytest
import requests
from dropbox.exceptions import AuthError

from dropbox_client_impl import DropboxAuthError, DropboxClient, DropboxConnectionError, auth
from dropbox_client_impl import connect as dropbox_connect

pytestmark = pytest.mark.usefixtures("isolated_env")

SETTINGS = auth.ConnectSettings("app-key", "app-secret", "https://web.test/connections/dropbox")


def _response(status: int, body: Mapping[str, object]) -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.ok = status == 200
    response.json.return_value = body
    return response


def _auth_error() -> AuthError:
    return AuthError("request-id", "invalid_access_token")


def test_connect_settings_need_all_three() -> None:
    """The connect settings are on only with the app key, secret and connect redirect URI."""
    assert auth.connect_settings() is None
    env = {
        auth.APP_KEY_ENV_VAR: "k",
        auth.APP_SECRET_ENV_VAR: "s",
        auth.CONNECT_REDIRECT_ENV_VAR: "https://web.test/d",
    }
    with patch.dict(os.environ, env):
        assert auth.connect_settings() == auth.ConnectSettings("k", "s", "https://web.test/d")


def test_the_authorization_url_asks_for_an_offline_token_with_pkce() -> None:
    """Our app, our redirect URI, the state, an S256 challenge, and a refresh token."""
    url = dropbox_connect.authorization_url(SETTINGS, "state-1", "challenge-1")

    parsed = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert (parsed.netloc, parsed.path) == ("www.dropbox.com", "/oauth2/authorize")
    assert query == {
        "client_id": "app-key",
        "redirect_uri": SETTINGS.redirect_uri,
        "response_type": "code",
        "token_access_type": "offline",
        "state": "state-1",
        "code_challenge": "challenge-1",
        "code_challenge_method": "S256",
    }


def test_exchange_code_returns_the_refresh_token_and_email() -> None:
    """The code and verifier are exchanged; the account's email is read for display."""
    body = {"refresh_token": "refresh-1", "account_id": "dbid:1", "scope": "files.content.write"}
    sdk = MagicMock()
    sdk.users_get_current_account.return_value.email = "alice@example.test"

    with (
        patch("requests.post", return_value=_response(200, body)) as post,
        patch("dropbox.Dropbox", return_value=sdk) as built,
    ):
        grant = dropbox_connect.exchange_code(SETTINGS, "code-1", "verifier-1")

    sent = post.call_args.kwargs["data"]
    assert (sent["code"], sent["code_verifier"]) == ("code-1", "verifier-1")
    assert sent["grant_type"] == "authorization_code"
    assert "client_secret" not in sent  # PKCE: the verifier, not the app secret
    assert built.call_args.kwargs["oauth2_refresh_token"] == "refresh-1"
    assert grant == dropbox_connect.DropboxGrant(
        "refresh-1",
        "dbid:1",
        "alice@example.test",
        "files.content.write",
    )


def test_exchange_code_still_connects_if_the_email_cant_be_read() -> None:
    """The email is only for display."""
    sdk = MagicMock()
    sdk.users_get_current_account.side_effect = requests.ConnectionError()

    with (
        patch("requests.post", return_value=_response(200, {"refresh_token": "r"})),
        patch("dropbox.Dropbox", return_value=sdk),
    ):
        assert dropbox_connect.exchange_code(SETTINGS, "c", "v").email == ""


@pytest.mark.parametrize("body", [{"error": "invalid_grant"}, {"access_token": "only"}])
def test_exchange_code_refused(body: dict[str, object]) -> None:
    """A refused code, or no refresh token, is an auth error with no token in it."""
    status = 400 if "error" in body else 200
    with (
        patch("requests.post", return_value=_response(status, body)),
        pytest.raises(DropboxAuthError) as raised,
    ):
        dropbox_connect.exchange_code(SETTINGS, "code-1", "verifier-1")

    assert "only" not in str(raised.value)


def test_exchange_code_unreachable() -> None:
    """A network failure is a connection error."""
    with (
        patch("requests.post", side_effect=requests.ConnectionError()),
        pytest.raises(DropboxConnectionError),
    ):
        dropbox_connect.exchange_code(SETTINGS, "code-1", "verifier-1")


def test_client_for_acts_as_the_person() -> None:
    """The client's SDK holds the person's refresh token and our app."""
    client = dropbox_connect.client_for(SETTINGS, "refresh-1")

    assert isinstance(client, DropboxClient)
    assert isinstance(client.dbx, dropbox.Dropbox)
    assert client.dbx._oauth2_refresh_token == "refresh-1"
    assert client.dbx._app_key == "app-key"


def test_revoke() -> None:
    """Revoke ends the token; an already-refused one is fine; anything else raises."""
    sdk = MagicMock()
    with patch("dropbox.Dropbox", return_value=sdk):
        dropbox_connect.revoke(SETTINGS, "refresh-1")
        sdk.auth_token_revoke.side_effect = _auth_error()
        dropbox_connect.revoke(SETTINGS, "refresh-1")
        sdk.auth_token_revoke.side_effect = requests.ConnectionError()
        with pytest.raises(DropboxConnectionError):
            dropbox_connect.revoke(SETTINGS, "refresh-1")

    assert sdk.auth_token_revoke.call_count == 3


def test_an_auth_error_is_found_through_the_contract_errors() -> None:
    """A refused token stays recognizable after the client and storage translate it."""
    wrapped = OSError("Dropbox failed")
    wrapped.__cause__ = OSError("translated")
    wrapped.__cause__.__cause__ = _auth_error()

    assert dropbox_connect.is_auth_failure(wrapped)

    assert not dropbox_connect.is_auth_failure(OSError("disk full"))
