"""Integration test for the delete route against a real Dropbox account.

Drives DELETE /files through the HTTP service, the DropboxClient and the real Dropbox API.
Fixtures are created and removed directly with the SDK inside a uniquely named scratch
folder, so the test leaves the account unchanged. Runs when any Dropbox credential is
available: a saved OAuth login, ``DROPBOX_REFRESH_TOKEN`` or ``DROPBOX_ACCESS_TOKEN``.
"""

import os
import uuid
from collections.abc import Iterator
from http import HTTPStatus

import dropbox
import pytest
from cloud_storage_service.app import app, get_storage_client
from fastapi.testclient import TestClient

from dropbox_client_impl import DropboxClient, auth

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            os.environ.get(auth.REFRESH_TOKEN_ENV_VAR)
            or os.environ.get(auth.ACCESS_TOKEN_ENV_VAR)
            or auth.get_token_file().exists()
        ),
        reason="Dropbox credentials are not configured.",
    ),
]


@pytest.fixture
def client() -> DropboxClient:
    """Return a DropboxClient authenticated from the environment."""
    return DropboxClient()


@pytest.fixture
def scratch(client: DropboxClient) -> Iterator[str]:
    """Create a scratch folder holding ``report.txt`` and ``Old/draft.txt``."""
    root = f"/ospsd-it-{uuid.uuid4().hex[:8]}"
    client.dbx.files_upload(b"hello", f"{root}/report.txt")
    client.dbx.files_upload(b"draft", f"{root}/Old/draft.txt")
    yield root
    client.dbx.files_delete_v2(root)


@pytest.fixture
def http(client: DropboxClient) -> Iterator[TestClient]:
    """Return an HTTP client for the service wired to the real DropboxClient."""
    app.dependency_overrides[get_storage_client] = lambda: client
    yield TestClient(app)
    app.dependency_overrides.clear()


def _exists(client: DropboxClient, path: str) -> bool:
    try:
        client.dbx.files_get_metadata(path)
    except dropbox.exceptions.ApiError:
        return False
    return True


def test_delete_file_then_repeat_returns_404(client: DropboxClient, scratch: str, http: TestClient) -> None:
    """A file is deleted (204), is gone afterwards, and a second delete returns 404."""
    path = f"{scratch}/report.txt"

    deleted = http.delete("/files", params={"path": path})
    assert deleted.status_code == HTTPStatus.NO_CONTENT, deleted.text
    assert not _exists(client, path)

    again = http.delete("/files", params={"path": path})
    assert again.status_code == HTTPStatus.NOT_FOUND, again.text


def test_delete_folder_removes_contents(client: DropboxClient, scratch: str, http: TestClient) -> None:
    """Deleting a non-empty folder removes it and everything inside it."""
    deleted = http.delete("/files", params={"path": f"{scratch}/Old"})

    assert deleted.status_code == HTTPStatus.NO_CONTENT, deleted.text
    assert not _exists(client, f"{scratch}/Old")
    assert not _exists(client, f"{scratch}/Old/draft.txt")
    assert _exists(client, f"{scratch}/report.txt")


def test_missing_path_returns_404(scratch: str, http: TestClient) -> None:
    """Deleting a path that never existed returns 404."""
    response = http.delete("/files", params={"path": f"{scratch}/nope.txt"})

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
