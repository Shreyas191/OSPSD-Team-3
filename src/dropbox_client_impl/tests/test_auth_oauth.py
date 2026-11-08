"""Unit tests for the OAuth authorization URL, its configuration, and ``finish_authorization``.

The real ``DropboxOAuth2Flow`` builds the authorization URL; only its private ``_finish``
(the HTTP call that exchanges the authorization code for tokens) is patched, and tests
check the code and redirect URI it was called with. Every test runs with an empty
environment and a token file in ``tmp_path`` (the ``isolated_env`` fixture), so the tests
can check exactly what was saved.
"""

import json
import os
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from dropbox import oauth
from tests.auth_helpers import (
    APP_KEY,
    APP_SECRET,
    OAUTH_ENV,
    REDIRECT_URI,
    REFRESH_TOKEN,
    assert_no_secrets,
    flow_result,
)

from dropbox_client_impl import auth

pytestmark = pytest.mark.usefixtures("isolated_env")


def _started_flow() -> tuple[oauth.DropboxOAuth2Flow, str]:
    session: dict[str, str] = {}
    flow = auth.get_authorization_flow(session)
    flow.start()
    return flow, session[auth.CSRF_SESSION_KEY]


# ----- OAuth authorization URL and configuration -----


@patch.dict(os.environ, OAUTH_ENV)
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


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        *[({k: v for k, v in OAUTH_ENV.items() if k != name}, name) for name in OAUTH_ENV],
        ({**OAUTH_ENV, "DROPBOX_APP_SECRET": "   "}, "DROPBOX_APP_SECRET"),
    ],
    ids=[*OAUTH_ENV, "blank-secret"],
)
def test_authorization_flow_missing_config_raises(env: dict[str, str], missing: str) -> None:
    """Each missing (or whitespace-only) OAuth variable is named in the error."""
    os.environ.update(env)

    with pytest.raises(auth.DropboxAuthError, match=missing) as exc_info:
        auth.get_authorization_flow({})

    assert_no_secrets(str(exc_info.value))


# ----- finish_authorization -----


@patch.dict(os.environ, OAUTH_ENV)
def test_finish_authorization_saves_refresh_token() -> None:
    """A valid redirect exchanges the code and stores only the refresh token locally."""
    flow, state = _started_flow()

    with patch.object(flow, "_finish", return_value=flow_result()) as mock_finish:
        account_id = auth.finish_authorization(flow, {"state": state, "code": "auth-code"})

    assert account_id == "dbid:1"
    mock_finish.assert_called_once_with("auth-code", REDIRECT_URI, None)
    saved = json.loads(auth.get_token_file().read_text())
    assert saved == {"refresh_token": REFRESH_TOKEN, "account_id": "dbid:1"}
    assert auth.load_refresh_token() == REFRESH_TOKEN


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"state": "STATE", "error": "access_denied"}, "denied"),
        ({"state": "forged-state-value-1234567890", "code": "auth-code"}, "authorization failed"),
        ({"code": "auth-code"}, "authorization failed"),
        (
            {"state": "STATE", "error": "server_error", "error_description": "oops"},
            "authorization failed",
        ),
    ],
    ids=["denied", "csrf-mismatch", "no-state", "provider-error"],
)
@patch.dict(os.environ, OAUTH_ENV)
def test_finish_authorization_invalid_redirect(params: dict[str, str], match: str) -> None:
    """Cancel in Dropbox, CSRF mismatches, malformed redirects and provider errors save nothing."""
    flow, state = _started_flow()
    params = {k: state if v == "STATE" else v for k, v in params.items()}

    with (
        patch.object(flow, "_finish") as mock_finish,
        pytest.raises(auth.DropboxAuthError, match=match),
    ):
        auth.finish_authorization(flow, params)

    mock_finish.assert_not_called()
    assert not auth.get_token_file().exists()


@patch.dict(os.environ, OAUTH_ENV)
def test_finish_authorization_rejected_code() -> None:
    """An expired/invalid authorization code is reported without leaking the secret."""
    flow, state = _started_flow()

    with (
        patch.object(flow, "_finish", side_effect=requests.HTTPError("400 Client Error")),
        pytest.raises(auth.DropboxAuthError, match="HTTPError") as exc_info,
    ):
        auth.finish_authorization(flow, {"state": state, "code": "expired-code"})

    assert_no_secrets(str(exc_info.value))
    assert not auth.get_token_file().exists()


@patch.dict(os.environ, OAUTH_ENV)
def test_finish_authorization_without_refresh_token() -> None:
    """A response without a refresh token is an error (we would be logged out in hours)."""
    flow, state = _started_flow()

    with (
        patch.object(flow, "_finish", return_value=flow_result(refresh_token=None)),
        pytest.raises(auth.DropboxAuthError, match="refresh token"),
    ):
        auth.finish_authorization(flow, {"state": state, "code": "auth-code"})

    assert not auth.get_token_file().exists()
