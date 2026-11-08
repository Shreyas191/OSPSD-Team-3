"""Unit tests for which Dropbox credentials ``auth`` and ``DropboxClient`` pick up.

Covers the token file, ``get_dropbox_client`` (refresh token vs. the manual access-token
fallback), ``get_access_token`` / ``get_auth_headers``, and ``DropboxClient`` construction.
The ``dropbox.Dropbox`` class is patched with ``autospec=True``, so a constructor call the
real SDK would reject fails the test. Every test runs with an empty environment and a token
file in ``tmp_path`` (the ``isolated_env`` fixture).
"""

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from dropbox.exceptions import AuthError
from tests.auth_helpers import (
    ACCESS_TOKEN,
    APP_KEY,
    APP_SECRET,
    OAUTH_ENV,
    REFRESH_TOKEN,
    assert_no_secrets,
    write_token_file,
)

from dropbox_client_impl import auth
from dropbox_client_impl.dropbox_impl import DropboxClient

pytestmark = pytest.mark.usefixtures("isolated_env")

PATCH_SDK = patch("dropbox_client_impl.auth.dropbox.Dropbox", autospec=True)


# ----- Token file -----


def test_get_token_file_default() -> None:
    """Without DROPBOX_TOKEN_FILE the token file is .dropbox_token.json."""
    del os.environ["DROPBOX_TOKEN_FILE"]
    assert auth.get_token_file() == Path(".dropbox_token.json")


def test_save_refresh_token_writes_private_file() -> None:
    """Saved credentials are JSON and readable only by the owner."""
    path = auth.save_refresh_token(REFRESH_TOKEN, "dbid:1")

    assert json.loads(path.read_text()) == {"refresh_token": REFRESH_TOKEN, "account_id": "dbid:1"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_refresh_token_from_file() -> None:
    """The refresh token is read from the token file."""
    write_token_file()
    assert auth.load_refresh_token() == REFRESH_TOKEN


def test_load_refresh_token_env_overrides_file() -> None:
    """DROPBOX_REFRESH_TOKEN takes precedence over the token file."""
    write_token_file("file-token")
    os.environ["DROPBOX_REFRESH_TOKEN"] = REFRESH_TOKEN
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


@PATCH_SDK
@patch.dict(os.environ, OAUTH_ENV)
def test_get_dropbox_client_uses_refresh_token(mock_dropbox: MagicMock) -> None:
    """With a saved refresh token the SDK client refreshes access tokens itself."""
    write_token_file()

    dbx = auth.get_dropbox_client()

    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token=REFRESH_TOKEN,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )
    assert dbx is mock_dropbox.return_value


@pytest.mark.parametrize("missing", ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"])
@PATCH_SDK
def test_get_dropbox_client_refresh_token_without_app_config_raises(
    mock_dropbox: MagicMock,
    missing: str,
) -> None:
    """A refresh token is useless without the app key and secret."""
    write_token_file()
    os.environ.update({k: v for k, v in OAUTH_ENV.items() if k != missing})

    with pytest.raises(auth.DropboxAuthError, match=missing) as exc_info:
        auth.get_dropbox_client()

    assert_no_secrets(str(exc_info.value))
    mock_dropbox.assert_not_called()


@PATCH_SDK
def test_get_dropbox_client_access_token_fallback(mock_dropbox: MagicMock) -> None:
    """DROPBOX_ACCESS_TOKEN is used only when no refresh token is available."""
    os.environ["DROPBOX_ACCESS_TOKEN"] = ACCESS_TOKEN

    auth.get_dropbox_client()

    mock_dropbox.assert_called_once_with(
        oauth2_access_token=ACCESS_TOKEN,
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )


@PATCH_SDK
@patch.dict(os.environ, OAUTH_ENV)
def test_get_dropbox_client_prefers_refresh_token(mock_dropbox: MagicMock) -> None:
    """OAuth credentials win over the manual access-token fallback."""
    write_token_file()
    os.environ["DROPBOX_ACCESS_TOKEN"] = ACCESS_TOKEN

    auth.get_dropbox_client()

    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token=REFRESH_TOKEN,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )


@PATCH_SDK
@patch.dict(os.environ, OAUTH_ENV)
def test_get_dropbox_client_not_authorized_raises(mock_dropbox: MagicMock) -> None:
    """Without any credentials the error tells the user to run the login command."""
    with pytest.raises(auth.DropboxAuthError, match="login"):
        auth.get_dropbox_client()
    mock_dropbox.assert_not_called()


# ----- get_access_token / headers -----


def test_get_access_token_with_fallback_token() -> None:
    """A manual access token is returned as-is (no refresh possible or needed)."""
    os.environ["DROPBOX_ACCESS_TOKEN"] = ACCESS_TOKEN
    assert auth.get_access_token() == ACCESS_TOKEN


@patch("dropbox_client_impl.auth.get_dropbox_client")
def test_get_access_token_refreshes(mock_client: MagicMock, dbx: MagicMock) -> None:
    """The SDK refreshes the short-lived access token before it is returned."""
    mock_client.return_value = dbx
    dbx._oauth2_access_token = ACCESS_TOKEN

    assert auth.get_access_token() == ACCESS_TOKEN
    dbx.check_and_refresh_access_token.assert_called_once_with()


@pytest.mark.parametrize(
    ("error", "expected", "match"),
    [
        (AuthError("req", "invalid_access_token"), auth.DropboxAuthError, "revoked or expired"),
        (requests.ConnectionError(), auth.DropboxConnectionError, "Could not reach Dropbox"),
    ],
)
@patch("dropbox_client_impl.auth.get_dropbox_client")
def test_get_access_token_refresh_failure(
    mock_client: MagicMock,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
    match: str,
) -> None:
    """A revoked refresh token or a network failure while refreshing raises without leaking it."""
    mock_client.return_value = dbx
    dbx.check_and_refresh_access_token.side_effect = error

    with pytest.raises(expected, match=match) as exc_info:
        auth.get_access_token()

    assert_no_secrets(str(exc_info.value))
    dbx.check_and_refresh_access_token.assert_called_once_with()


def test_get_auth_headers() -> None:
    """The Authorization header uses the Bearer scheme."""
    os.environ["DROPBOX_ACCESS_TOKEN"] = ACCESS_TOKEN
    assert auth.get_auth_headers() == {"Authorization": f"Bearer {ACCESS_TOKEN}"}


# ----- DropboxClient -----


@PATCH_SDK
def test_init_with_provided_instance_skips_auth(mock_dropbox: MagicMock, dbx: MagicMock) -> None:
    """Passing a Dropbox instance uses it directly and skips auth."""
    client = DropboxClient(dbx=dbx)

    assert client.dbx is dbx
    mock_dropbox.assert_not_called()


@PATCH_SDK
@patch.dict(os.environ, OAUTH_ENV)
def test_init_uses_get_dropbox_client(mock_dropbox: MagicMock) -> None:
    """DropboxClient gets its OAuth-backed SDK instance from get_dropbox_client."""
    write_token_file()

    client = DropboxClient()

    assert client.dbx is mock_dropbox.return_value
    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token=REFRESH_TOKEN,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )


@PATCH_SDK
def test_init_not_authorized_raises(mock_dropbox: MagicMock) -> None:
    """DropboxClient fails clearly when the user has not logged in."""
    with pytest.raises(auth.DropboxAuthError, match="login"):
        DropboxClient()

    mock_dropbox.assert_not_called()
