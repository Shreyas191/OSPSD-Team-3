"""Integration tests for the create routes against a real Dropbox account.

Drives POST /files/upload, POST /folders/create and POST /files/copy through the HTTP
service, the DropboxClient and the real Dropbox API. Everything happens inside a uniquely
named scratch folder that is deleted afterwards, so the account is left unchanged. Runs
when any Dropbox credential is available: a saved OAuth login, ``DROPBOX_REFRESH_TOKEN``
or ``DROPBOX_ACCESS_TOKEN``.
"""

import os
import uuid
from collections.abc import Iterator
from http import HTTPStatus
from typing import Any

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

CONTENT = b"%PDF-1.7 integration test"


@pytest.fixture
def client() -> DropboxClient:
    """Return a DropboxClient authenticated from the environment."""
    return DropboxClient()


@pytest.fixture
def scratch(client: DropboxClient) -> Iterator[str]:
    """Return the path of an empty scratch folder, deleted with its contents afterwards."""
    root = f"/ospsd-it-{uuid.uuid4().hex[:8]}"
    client.dbx.files_create_folder_v2(root)
    yield root
    client.dbx.files_delete_v2(root)


@pytest.fixture
def http(client: DropboxClient) -> Iterator[TestClient]:
    """Return an HTTP client for the service wired to the real DropboxClient."""
    app.dependency_overrides[get_storage_client] = lambda: client
    yield TestClient(app)
    app.dependency_overrides.clear()


def _upload(http: TestClient, remote_path: str, content: bytes, *, overwrite: bool = False) -> dict[str, Any]:
    response = http.post(
        "/files/upload",
        files={"file": ("report.pdf", content)},
        data={"remote_path": remote_path, "overwrite": str(overwrite).lower()},
    )
    assert response.status_code == HTTPStatus.OK, response.text
    body: dict[str, Any] = response.json()
    return body


def _download(client: DropboxClient, path: str) -> bytes:
    _, response = client.dbx.files_download(path)
    return bytes(response.content)


def test_upload_then_overwrite_round_trip(client: DropboxClient, scratch: str, http: TestClient) -> None:
    """Uploaded bytes match in Dropbox; overwrite replaces them and keeps the file's id."""
    path = f"{scratch}/Docs/report.pdf"

    first = _upload(http, path, CONTENT)
    assert (first["path"], first["size"], first["is_folder"]) == (path, len(CONTENT), False)
    assert _download(client, path) == CONTENT

    second = _upload(http, path, b"replaced", overwrite=True)
    assert second["id"] == first["id"]
    assert _download(client, path) == b"replaced"


def test_upload_onto_existing_file_without_overwrite_is_409(
    client: DropboxClient,
    scratch: str,
    http: TestClient,
) -> None:
    """A different upload to a taken path is a 409 conflict, and the original bytes stay."""
    path = f"{scratch}/report.pdf"
    _upload(http, path, CONTENT)

    response = http.post(
        "/files/upload",
        files={"file": ("report.pdf", b"different")},
        data={"remote_path": path},
    )

    assert response.status_code == HTTPStatus.CONFLICT, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert _download(client, path) == CONTENT


def test_create_folder_then_conflict(scratch: str, http: TestClient) -> None:
    """A new folder (with missing parents) is created; creating it again is a 409."""
    path = f"{scratch}/A/B"

    created = http.post("/folders/create", json={"remote_path": path})
    assert created.status_code == HTTPStatus.OK, created.text
    assert (created.json()["path"], created.json()["is_folder"]) == (path, True)

    again = http.post("/folders/create", json={"remote_path": path})
    assert again.status_code == HTTPStatus.CONFLICT, again.text
    assert again.json()["error"]["code"] == "conflict"


def test_copy_round_trip_and_failures(client: DropboxClient, scratch: str, http: TestClient) -> None:
    """A copy gets a new id and the same bytes; missing, taken and nested copies are rejected."""
    source = f"{scratch}/Docs/report.pdf"
    original = _upload(http, source, CONTENT)

    copied = http.post("/files/copy", json={"from_path": source, "to_path": f"{scratch}/Archive/report.pdf"})
    assert copied.status_code == HTTPStatus.OK, copied.text
    assert copied.json()["id"] != original["id"]
    assert _download(client, f"{scratch}/Archive/report.pdf") == CONTENT
    assert _download(client, source) == CONTENT

    missing = http.post("/files/copy", json={"from_path": f"{scratch}/nope.pdf", "to_path": f"{scratch}/x.pdf"})
    assert missing.status_code == HTTPStatus.NOT_FOUND, missing.text
    assert missing.json()["error"]["code"] == "not_found"

    taken = http.post("/files/copy", json={"from_path": source, "to_path": f"{scratch}/Archive/report.pdf"})
    assert taken.status_code == HTTPStatus.CONFLICT, taken.text
    assert taken.json()["error"]["code"] == "conflict"

    nested = http.post("/files/copy", json={"from_path": f"{scratch}/Docs", "to_path": f"{scratch}/Docs/Inner"})
    assert nested.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, nested.text
    assert nested.json()["error"]["code"] == "invalid_input"
    with pytest.raises(dropbox.exceptions.ApiError):
        client.dbx.files_get_metadata(f"{scratch}/Docs/Inner")
