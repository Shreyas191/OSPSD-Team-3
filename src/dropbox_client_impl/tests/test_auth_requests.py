"""Unit tests for authenticated Dropbox HTTP calls: ``dropbox_request`` and ``check_auth``.

``requests.post`` is patched with ``autospec=True`` and returns ``Mock(spec=requests.Response)``
objects, and each test checks the URL, headers and body that were posted, so no request
reaches Dropbox. Every test runs with an empty environment and a token file in ``tmp_path``
(the ``isolated_env`` fixture); most add the manual ``DROPBOX_ACCESS_TOKEN`` so no token
refresh is needed.
"""

import logging
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests
from tests.auth_helpers import ACCESS_TOKEN, assert_no_secrets

from dropbox_client_impl import auth

pytestmark = pytest.mark.usefixtures("isolated_env")

ACCOUNT = {"name": {"display_name": "Ada"}, "email": "ada@example.com"}


@pytest.fixture
def access_token_env(isolated_env: None) -> Iterator[None]:
    """Use the manual access-token fallback so requests need no refresh call.

    Depends on ``isolated_env`` so the variable is set after the environment is cleared.
    """
    with patch.dict(os.environ, {"DROPBOX_ACCESS_TOKEN": ACCESS_TOKEN}):
        yield


@pytest.fixture
def mock_post() -> Iterator[MagicMock]:
    """Patch ``requests.post`` in ``auth`` with a mock that has the real signature."""
    with patch("dropbox_client_impl.auth.requests.post", autospec=True) as post:
        yield post


def _response(status_code: int, json_body: Any = None, text: str = "") -> Mock:
    response = Mock(spec=requests.Response)
    response.status_code = status_code
    response.ok = status_code < 400
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


def _assert_posted(mock_post: MagicMock, endpoint: str, payload: Any = None) -> None:
    mock_post.assert_called_once_with(
        f"https://api.dropboxapi.com/2/{endpoint}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        json=payload,
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )


# ----- dropbox_request -----


@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_success(mock_post: MagicMock) -> None:
    """A successful call posts to the right URL with auth and returns JSON."""
    mock_post.return_value = _response(200, {"ok": True})

    result = auth.dropbox_request("/files/get_metadata", {"path": "/a"})

    assert result == {"ok": True}
    _assert_posted(mock_post, "files/get_metadata", {"path": "/a"})


@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_empty_body_returns_none(mock_post: MagicMock) -> None:
    """An empty response body returns None."""
    mock_post.return_value = _response(200, text="")

    assert auth.dropbox_request("users/get_space_usage") is None
    _assert_posted(mock_post, "users/get_space_usage")


@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_invalid_token_raises_auth_error(mock_post: MagicMock) -> None:
    """HTTP 401 (invalid/expired token) raises DropboxAuthError pointing to login."""
    mock_post.return_value = _response(401, {"error_summary": "expired_access_token/"})

    with pytest.raises(auth.DropboxAuthError, match="expired_access_token") as exc_info:
        auth.dropbox_request("/users/get_current_account")

    assert "login" in str(exc_info.value)
    assert_no_secrets(str(exc_info.value))
    _assert_posted(mock_post, "users/get_current_account")


@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_api_error_json(mock_post: MagicMock) -> None:
    """Other error responses raise DropboxAPIError with the status code."""
    mock_post.return_value = _response(409, {"error_summary": "path/not_found/"})

    with pytest.raises(auth.DropboxAPIError, match="path/not_found") as exc_info:
        auth.dropbox_request("/files/get_metadata", {"path": "/missing"})

    assert exc_info.value.status_code == 409
    _assert_posted(mock_post, "files/get_metadata", {"path": "/missing"})


@pytest.mark.parametrize(
    ("json_body", "text", "expected"),
    [
        (None, "Error in call to API function: bad input", "bad input"),
        (None, "", "Reason"),
        ({"error": "boom"}, "", "boom"),
        (["unexpected"], "", "unexpected"),
    ],
)
@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_api_error_message_formats(
    mock_post: MagicMock,
    json_body: Any,
    text: str,
    expected: str,
) -> None:
    """Error messages are extracted from JSON or plain-text bodies."""
    mock_post.return_value = _response(400, json_body, text)

    with pytest.raises(auth.DropboxAPIError, match=expected):
        auth.dropbox_request("/x")

    _assert_posted(mock_post, "x")


@pytest.mark.usefixtures("access_token_env")
def test_dropbox_request_network_error(mock_post: MagicMock) -> None:
    """Network failures raise DropboxConnectionError."""
    mock_post.side_effect = requests.ConnectionError("dns failure")

    with pytest.raises(auth.DropboxConnectionError, match="dns failure"):
        auth.dropbox_request("/users/get_current_account")

    _assert_posted(mock_post, "users/get_current_account")


def test_dropbox_request_without_credentials_makes_no_request(mock_post: MagicMock) -> None:
    """No HTTP request is made without credentials."""
    with pytest.raises(auth.DropboxAuthError):
        auth.dropbox_request("/users/get_current_account")

    mock_post.assert_not_called()


# ----- get_current_account / check_auth -----


@patch("dropbox_client_impl.auth.dropbox_request", autospec=True)
def test_get_current_account(mock_request: MagicMock) -> None:
    """get_current_account calls users/get_current_account."""
    mock_request.return_value = {"account_id": "dbid:1"}

    assert auth.get_current_account() == {"account_id": "dbid:1"}
    mock_request.assert_called_once_with("/users/get_current_account")


@patch("dropbox_client_impl.auth.get_current_account", autospec=True)
def test_check_auth_success(mock_account: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    """check_auth reports success with the account name."""
    mock_account.return_value = ACCOUNT

    assert auth.check_auth() is True
    assert "authentication OK: Ada <ada@example.com>" in capsys.readouterr().out
    mock_account.assert_called_once_with()


@patch(
    "dropbox_client_impl.auth.get_current_account",
    autospec=True,
    side_effect=auth.DropboxAuthError("bad token"),
)
def test_check_auth_failure(mock_account: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    """check_auth reports the error and returns False."""
    assert auth.check_auth() is False
    assert "authentication FAILED: bad token" in capsys.readouterr().out
    mock_account.assert_called_once_with()


@patch("dropbox_client_impl.auth.get_dropbox_client", autospec=True)
def test_check_auth_with_refresh_token_end_to_end(
    mock_client: MagicMock,
    mock_post: MagicMock,
    dbx: MagicMock,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """check_auth refreshes, calls the API with the fresh token, and never prints it."""
    caplog.set_level(logging.DEBUG)
    mock_client.return_value = dbx
    dbx._oauth2_access_token = ACCESS_TOKEN
    mock_post.return_value = _response(200, ACCOUNT)

    assert auth.check_auth() is True

    dbx.check_and_refresh_access_token.assert_called_once_with()
    _assert_posted(mock_post, "users/get_current_account")
    assert_no_secrets(capsys.readouterr().out, caplog.text)
