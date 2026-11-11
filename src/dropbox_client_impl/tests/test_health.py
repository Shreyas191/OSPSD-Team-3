"""Unit tests for ``DropboxClient.check_health``.

The Dropbox SDK is an autospec mock (see ``conftest.py``); each test checks the single
``users_get_current_account`` call and the documented exception each failure becomes.
"""

from unittest.mock import MagicMock

import pytest
import requests
from dropbox import auth, exceptions
from tests.dropbox_sdk import rate_limited, server_error

from dropbox_client_impl import DropboxClient

SECRET = "sl.secret-token-value"


def test_check_health_calls_get_current_account_once(client: DropboxClient, dbx: MagicMock) -> None:
    """A healthy account makes exactly one users_get_current_account call and returns None."""
    result = client.check_health()  # type: ignore[func-returns-value]

    assert result is None
    dbx.users_get_current_account.assert_called_once_with()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (rate_limited(), ConnectionError),
        (requests.exceptions.ConnectionError("connection refused"), ConnectionError),
        (requests.exceptions.ReadTimeout("read timed out"), ConnectionError),
        (server_error(), OSError),
        (exceptions.AuthError("request-id", auth.AuthError.invalid_access_token), OSError),
    ],
)
def test_check_health_errors_become_the_documented_exceptions(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes a health check: each failure becomes one documented built-in."""
    dbx.users_get_current_account.side_effect = error

    with pytest.raises(expected) as raised:
        client.check_health()

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error
    assert "the Dropbox account" in str(raised.value)


def test_check_health_auth_failure_message_has_no_token(dbx: MagicMock) -> None:
    """Revoked credentials become an OSError whose message never contains the token."""
    dbx._oauth2_access_token = SECRET  # where the real SDK keeps the token
    dbx.users_get_current_account.side_effect = exceptions.AuthError(
        "request-id",
        auth.AuthError.expired_access_token,
    )

    with pytest.raises(OSError, match="Dropbox failed checking the Dropbox account") as raised:
        DropboxClient(dbx=dbx).check_health()

    assert SECRET not in str(raised.value)
    assert "sl." not in str(raised.value)
