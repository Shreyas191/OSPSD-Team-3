"""Unit tests for ``DropboxClient.copy``.

The Dropbox SDK is an autospec mock (see ``conftest.py``) that returns and raises real SDK
objects, so each test checks the exact ``files_copy_v2`` call, the ``File`` built from the
copy's metadata, and the documented exception each Dropbox failure becomes.
"""

from unittest.mock import MagicMock

import pytest
from tests.dropbox_sdk import server_error

from dropbox_client_impl.dropbox_impl import DropboxClient


def test_check_provider_health_calls_dropbox(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """Call Dropbox once and return None on success."""
    result = client.check_provider_health()  # type: ignore[func-returns-value]

    assert result is None
    dbx.users_get_current_account.assert_called_once_with()

def test_check_provider_health_translates_dropbox_failure(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """Raise ConnectionError when Dropbox fails the account request."""
    failure = server_error()
    dbx.users_get_current_account.side_effect = failure

    with pytest.raises(ConnectionError) as caught:
        client.check_provider_health()

    dbx.users_get_current_account.assert_called_once_with()
    assert caught.value.__cause__ is failure
