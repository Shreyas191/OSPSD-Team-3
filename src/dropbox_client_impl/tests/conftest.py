"""Fixtures shared by the ``dropbox_client_impl`` unit tests."""

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import dropbox
import pytest

from dropbox_client_impl import DropboxClient


@pytest.fixture
def dbx() -> MagicMock:
    """Return a fake Dropbox SDK client with the real ``dropbox.Dropbox`` method signatures.

    A call whose arguments the real SDK method would not accept fails the test, so tests
    check the SDK call the client makes, not just its result.
    """
    sdk: MagicMock = create_autospec(dropbox.Dropbox, instance=True)
    return sdk


@pytest.fixture
def client(dbx: MagicMock) -> DropboxClient:
    """Return a ``DropboxClient`` that talks to the fake SDK."""
    return DropboxClient(dbx=dbx)


@pytest.fixture
def isolated_env(tmp_path: Path) -> Iterator[None]:
    """Clear all environment variables and point the token file at ``tmp_path``.

    Auth tests opt in with ``pytestmark = pytest.mark.usefixtures("isolated_env")`` so a
    developer's real ``DROPBOX_*`` variables or ``.dropbox_token.json`` never leak in.
    """
    with patch.dict(os.environ, {"DROPBOX_TOKEN_FILE": str(tmp_path / "token.json")}, clear=True):
        yield
