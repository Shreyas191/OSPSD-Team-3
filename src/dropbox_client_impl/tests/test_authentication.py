"""Unit tests for DropboxClient construction and authentication.

All Dropbox SDK calls are mocked, so these tests need no network or credentials.
"""

import os
from typing import Any
from unittest.mock import Mock, patch

import pytest

from dropbox_client_impl.dropbox_impl import DropboxClient

ENV = {
    "DROPBOX_APP_KEY": "test_app_key",
    "DROPBOX_APP_SECRET": "test_app_secret",
    "DROPBOX_REFRESH_TOKEN": "test_refresh_token",
}


@patch("dropbox_client_impl.dropbox_impl.dropbox.Dropbox")
def test_init_with_provided_instance_skips_auth(mock_dropbox: Any) -> None:
    """Passing a Dropbox instance uses it directly and skips auth."""
    dbx = Mock()

    client = DropboxClient(dbx=dbx)

    assert client.dbx is dbx
    mock_dropbox.assert_not_called()


@patch("dropbox_client_impl.dropbox_impl.dropbox.Dropbox")
@patch.dict(os.environ, ENV, clear=True)
def test_init_with_env_vars(mock_dropbox: Any) -> None:
    """Credentials from the environment are passed to the Dropbox SDK."""
    client = DropboxClient()

    mock_dropbox.assert_called_once_with(
        oauth2_refresh_token="test_refresh_token",
        app_key="test_app_key",
        app_secret="test_app_secret",
    )
    assert client.dbx is mock_dropbox.return_value


@pytest.mark.parametrize("missing", list(ENV))
@patch("dropbox_client_impl.dropbox_impl.dropbox.Dropbox")
def test_init_missing_env_var_raises(mock_dropbox: Any, missing: str) -> None:
    """A clear error names any missing environment variable."""
    env = {key: value for key, value in ENV.items() if key != missing}

    with patch.dict(os.environ, env, clear=True), pytest.raises(RuntimeError, match=missing):
        DropboxClient()

    mock_dropbox.assert_not_called()
