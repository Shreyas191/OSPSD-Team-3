"""Integration test for the update routes against a real Dropbox account.

Drives POST /files/rename and POST /files/move through the HTTP service, the
DropboxClient and the real Dropbox API. Fixtures are created and removed directly with
the SDK inside a uniquely named scratch folder, so the test leaves the account unchanged.
Runs when any Dropbox credential is available: a saved OAuth login, ``DROPBOX_REFRESH_TOKEN``
or ``DROPBOX_ACCESS_TOKEN``.
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
    """Create a scratch folder holding ``report.txt`` and an empty ``Archive`` folder."""
    root = f"/ospsd-it-{uuid.uuid4().hex[:8]}"
    client.dbx.files_upload(b"hello", f"{root}/report.txt")
    client.dbx.files_create_folder_v2(f"{root}/Archive")
    yield root
    client.dbx.files_delete_v2(root)


@pytest.fixture
def http(client: DropboxClient) -> Iterator[TestClient]:
    """Return an HTTP client for the service wired to the real DropboxClient."""
    app.dependency_overrides[get_storage_client] = lambda: client
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_rename_then_move_round_trip(client: DropboxClient, scratch: str, http: TestClient) -> None:
    """A file can be renamed in place and then moved into another folder."""
    renamed = http.post("/files/rename", json={"path": f"{scratch}/report.txt", "new_name": "summary.txt"})
    assert renamed.status_code == HTTPStatus.OK, renamed.text
    assert renamed.json()["path"] == f"{scratch}/summary.txt"
    assert renamed.json()["size"] == len(b"hello")

    moved = http.post("/files/move", json={"from_path": f"{scratch}/summary.txt", "to_path": f"{scratch}/Archive/summary.txt"})
    assert moved.status_code == HTTPStatus.OK, moved.text
    assert moved.json()["path"] == f"{scratch}/Archive/summary.txt"
    assert moved.json()["id"] == renamed.json()["id"]

    with pytest.raises(dropbox.exceptions.ApiError):
        client.dbx.files_get_metadata(f"{scratch}/report.txt")
    assert client.dbx.files_get_metadata(f"{scratch}/Archive/summary.txt").size == len(b"hello")


def test_missing_source_and_conflict(scratch: str, http: TestClient) -> None:
    """Missing sources are 404; occupied destinations (including the source itself) are 409."""
    missing = http.post("/files/move", json={"from_path": f"{scratch}/nope.txt", "to_path": f"{scratch}/x.txt"})
    assert missing.status_code == HTTPStatus.NOT_FOUND

    conflict = http.post("/files/rename", json={"path": f"{scratch}/report.txt", "new_name": "Archive"})
    assert conflict.status_code == HTTPStatus.CONFLICT

    same_name = http.post("/files/rename", json={"path": f"{scratch}/report.txt", "new_name": "report.txt"})
    assert same_name.status_code == HTTPStatus.CONFLICT

    case_only = http.post("/files/rename", json={"path": f"{scratch}/report.txt", "new_name": "Report.txt"})
    assert case_only.status_code == HTTPStatus.CONFLICT
