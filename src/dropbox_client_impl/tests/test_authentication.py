"""Unit tests for Dropbox authentication and DropboxClient construction.

All HTTP and Dropbox SDK calls are mocked, so these tests need no network or credentials.
"""

import os
from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests

from dropbox_client_impl import auth
from dropbox_client_impl.dropbox_impl import DropboxClient

ENV = {"DROPBOX_ACCESS_TOKEN": "test_access_token"}
HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409


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


# ----- Token and headers -----


@patch.dict(os.environ, ENV, clear=True)
def test_get_access_token_reads_env() -> None:
    """The token is read from DROPBOX_ACCESS_TOKEN."""
    assert auth.get_access_token() == "test_access_token"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_get_access_token_missing_raises(value: str | None) -> None:
    """A missing or blank token raises a clear auth error."""
    env = {} if value is None else {"DROPBOX_ACCESS_TOKEN": value}
    with (
        patch.dict(os.environ, env, clear=True),
        pytest.raises(auth.DropboxAuthError, match="DROPBOX_ACCESS_TOKEN"),
    ):
        auth.get_access_token()


@patch.dict(os.environ, ENV, clear=True)
def test_get_auth_headers() -> None:
    """The Authorization header uses the Bearer scheme."""
    assert auth.get_auth_headers() == {"Authorization": "Bearer test_access_token"}


# ----- dropbox_request -----


@patch("dropbox_client_impl.auth.requests.post")
@patch.dict(os.environ, ENV, clear=True)
def test_dropbox_request_success(mock_post: Any) -> None:
    """A successful call posts to the right URL with auth and returns JSON."""
    mock_post.return_value = _response(200, {"ok": True})

    result = auth.dropbox_request("/files/get_metadata", {"path": "/a"})

    assert result == {"ok": True}
    mock_post.assert_called_once_with(
        "https://api.dropboxapi.com/2/files/get_metadata",
        headers={"Authorization": "Bearer test_access_token"},
        json={"path": "/a"},
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )


@patch("dropbox_client_impl.auth.requests.post")
@patch.dict(os.environ, ENV, clear=True)
def test_dropbox_request_empty_body_returns_none(mock_post: Any) -> None:
    """An empty response body returns None."""
    mock_post.return_value = _response(200, text="")

    assert auth.dropbox_request("users/get_space_usage") is None


@patch("dropbox_client_impl.auth.requests.post")
@patch.dict(os.environ, ENV, clear=True)
def test_dropbox_request_invalid_token_raises_auth_error(mock_post: Any) -> None:
    """HTTP 401 (invalid/expired token) raises DropboxAuthError."""
    mock_post.return_value = _response(401, {"error_summary": "expired_access_token/"})

    with pytest.raises(auth.DropboxAuthError, match="expired_access_token"):
        auth.dropbox_request("/users/get_current_account")


@patch("dropbox_client_impl.auth.requests.post")
@patch.dict(os.environ, ENV, clear=True)
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
@patch.dict(os.environ, ENV, clear=True)
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
@patch.dict(os.environ, ENV, clear=True)
def test_dropbox_request_network_error(mock_post: Any) -> None:
    """Network failures raise DropboxConnectionError."""
    with pytest.raises(auth.DropboxConnectionError, match="dns failure"):
        auth.dropbox_request("/users/get_current_account")


@patch("dropbox_client_impl.auth.requests.post")
def test_dropbox_request_missing_token_makes_no_request(mock_post: Any) -> None:
    """No HTTP request is made without a token."""
    with patch.dict(os.environ, {}, clear=True), pytest.raises(auth.DropboxAuthError):
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


# ----- SDK client / DropboxClient -----


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
@patch.dict(os.environ, ENV, clear=True)
def test_get_dropbox_client_uses_access_token(mock_dropbox: Any) -> None:
    """The SDK client is built from the access token."""
    dbx = auth.get_dropbox_client()

    mock_dropbox.assert_called_once_with(
        oauth2_access_token="test_access_token",
        timeout=auth.REQUEST_TIMEOUT_SECONDS,
    )
    assert dbx is mock_dropbox.return_value


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
def test_init_with_provided_instance_skips_auth(mock_dropbox: Any) -> None:
    """Passing a Dropbox instance uses it directly and skips auth."""
    dbx = Mock()

    client = DropboxClient(dbx=dbx)

    assert client.dbx is dbx
    mock_dropbox.assert_not_called()


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
@patch.dict(os.environ, ENV, clear=True)
def test_init_uses_get_dropbox_client(mock_dropbox: Any) -> None:
    """DropboxClient gets its SDK instance from get_dropbox_client."""
    client = DropboxClient()

    assert client.dbx is mock_dropbox.return_value


@patch("dropbox_client_impl.auth.dropbox.Dropbox")
def test_init_missing_token_raises(mock_dropbox: Any) -> None:
    """DropboxClient fails clearly when no token is configured."""
    with (
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(auth.DropboxAuthError, match="DROPBOX_ACCESS_TOKEN"),
    ):
        DropboxClient()

    mock_dropbox.assert_not_called()
