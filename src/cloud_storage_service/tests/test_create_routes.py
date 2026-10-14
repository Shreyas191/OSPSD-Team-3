"""HTTP contract tests for the create routes (upload, create folder, copy).

The storage client is replaced through ``app.dependency_overrides`` with the in-memory
``FakeStorageClient`` from ``tests/fakes.py``, so these tests need no Dropbox credentials
and no network. They check status codes, response bodies, and what storage holds
afterwards.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import cloud_storage_client_api
import pytest
from fastapi import status
from fastapi.testclient import TestClient
from httpx import Response
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import FileResponse, app, get_storage_client

MODIFIED = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


class RecordingStorageClient(FakeStorageClient):
    """A fake that also records what the upload route handed to ``upload_file``."""

    def __init__(self) -> None:
        """Start empty, with nothing recorded."""
        super().__init__()
        self.uploaded_bytes: bytes | None = None
        self.uploaded_from: str | None = None

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = False,
    ) -> cloud_storage_client_api.File:
        """Record the local file, which only exists while the route is handling the request."""
        self.uploaded_from = local_path
        self.uploaded_bytes = Path(local_path).read_bytes()
        return super().upload_file(local_path, remote_path, overwrite=overwrite)


@pytest.fixture
def storage() -> Iterator[RecordingStorageClient]:
    """Install an empty fake storage client for the duration of a test."""
    client = RecordingStorageClient()
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Create a client without starting the real Dropbox lifespan."""
    # Deliberately not used as a context manager: that would run the lifespan and try to
    # build a real DropboxClient.
    return TestClient(app)


# --- FileResponse.from_file: domain File -> HTTP response model -----------------------


def make_file(path: str = "/Docs/report.pdf", *, is_folder: bool = False) -> FileResponse:
    """Build a response from a stand-in for any ``cloud_storage_client_api.File``."""
    file = SimpleNamespace(
        id="id:abc123",
        name=path.rsplit("/", 1)[-1],
        path=path,
        is_folder=is_folder,
        size=None if is_folder else 2048,
        modified=None if is_folder else MODIFIED,
    )
    return FileResponse.from_file(cast("cloud_storage_client_api.File", file))


def test_from_file_maps_every_field() -> None:
    """Map every domain file field into the response model."""
    assert make_file().model_dump() == {
        "id": "id:abc123",
        "name": "report.pdf",
        "path": "/Docs/report.pdf",
        "is_folder": False,
        "size": 2048,
        "modified": MODIFIED,
    }


def test_from_file_folder_has_null_size_and_modified() -> None:
    """Keep folder size and modification time null in the response."""
    response = make_file("/Docs", is_folder=True)
    assert (response.is_folder, response.size, response.modified) == (True, None, None)


# --- POST /folders/create ----------------------------------------------------------------


def test_create_folder_returns_new_folder(storage: FakeStorageClient, http: TestClient) -> None:
    """Creating a folder returns it with null size and modified, and stores it."""
    response = http.post("/folders/create", json={"remote_path": "/Docs/New"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["path"], body["is_folder"], body["size"], body["modified"]) == (
        "/Docs/New",
        True,
        None,
        None,
    )
    assert storage.entries["/Docs/New"].is_folder


def test_create_existing_folder_returns_409(storage: FakeStorageClient, http: TestClient) -> None:
    """Creating a folder where something already exists returns 409 and changes nothing."""
    existing = storage.add("/Docs", is_folder=True)

    response = http.post("/folders/create", json={"remote_path": "/Docs"})

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json() == {"detail": "Something already exists at '/Docs'"}
    assert storage.entries == {"/Docs": existing}


# --- POST /files/copy --------------------------------------------------------------------


def test_copy_file_returns_copy_and_keeps_source(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Copying a file returns a new entry at the destination; the source is unchanged."""
    source = storage.add("/Docs/a.pdf", size=2048)

    response = http.post("/files/copy", json={"from_path": "/Docs/a.pdf", "to_path": "/Copy/a.pdf"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["path"], body["size"], body["is_folder"]) == ("/Copy/a.pdf", 2048, False)
    assert body["id"] != source.id
    assert storage.entries["/Docs/a.pdf"] is source


def test_copy_folder_copies_contents(storage: FakeStorageClient, http: TestClient) -> None:
    """The same route copies a folder together with its contents."""
    storage.add("/Docs", is_folder=True)
    storage.add("/Docs/a.pdf")

    response = http.post("/files/copy", json={"from_path": "/Docs", "to_path": "/Backup"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_folder"] is True
    assert set(storage.entries) == {"/Docs", "/Docs/a.pdf", "/Backup", "/Backup/a.pdf"}


def test_copy_missing_source_returns_404(storage: FakeStorageClient, http: TestClient) -> None:
    """Copying something that does not exist returns 404."""
    response = http.post("/files/copy", json={"from_path": "/missing.pdf", "to_path": "/b.pdf"})

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {"detail": "No file or folder at '/missing.pdf'"}
    assert storage.entries == {}


def test_copy_onto_existing_destination_returns_409(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Copying onto an existing entry returns 409 and overwrites nothing."""
    a = storage.add("/a.pdf", size=1)
    b = storage.add("/b.pdf", size=2)

    response = http.post("/files/copy", json={"from_path": "/a.pdf", "to_path": "/b.pdf"})

    assert response.status_code == status.HTTP_409_CONFLICT
    assert storage.entries == {"/a.pdf": a, "/b.pdf": b}


@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("/files/copy", {"from_path": "/a"}),
        ("/files/copy", {"from_path": "a.pdf", "to_path": "/b.pdf"}),
        ("/folders/create", {"remote_path": "NoSlash"}),
    ],
)
def test_invalid_request_is_rejected_and_changes_nothing(
    storage: FakeStorageClient,
    http: TestClient,
    url: str,
    body: dict[str, str],
) -> None:
    """Missing fields and relative paths return 422 before reaching storage."""
    storage.add("/a", is_folder=True)

    response = http.post(url, json=body)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert set(storage.entries) == {"/a"}


# --- POST /files/upload (multipart) --------------------------------------------------------


def post_upload(http: TestClient, content: bytes = b"%PDF-data", **form_fields: str) -> Response:
    """Post a multipart upload request with the supplied form fields."""
    data = {"remote_path": "/Docs/report.pdf", **form_fields}
    response: Response = http.post(
        "/files/upload",
        files={"file": ("report.pdf", content)},
        data=data,
    )
    return response


def test_upload_stores_sent_bytes_and_cleans_up(
    storage: RecordingStorageClient,
    http: TestClient,
) -> None:
    """The client receives exactly the uploaded bytes, and the temporary file is removed."""
    response = post_upload(http)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["path"] == "/Docs/report.pdf"
    assert response.json()["size"] == len(b"%PDF-data")
    assert storage.uploaded_bytes == b"%PDF-data"
    assert storage.uploaded_from is not None
    assert not Path(storage.uploaded_from).exists()


def test_upload_existing_file_without_overwrite_returns_409(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Without ``overwrite`` (the default), an existing file is not replaced."""
    existing = storage.add("/Docs/report.pdf", size=1)

    response = post_upload(http)

    assert response.status_code == status.HTTP_409_CONFLICT
    assert storage.entries["/Docs/report.pdf"] is existing


def test_upload_with_overwrite_replaces_existing_file(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """With ``overwrite=true`` the file is replaced and keeps its ID."""
    existing = storage.add("/Docs/report.pdf", size=1)

    response = post_upload(http, b"new content", overwrite="true")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == existing.id
    assert response.json()["size"] == len(b"new content")


def test_upload_rejects_invalid_remote_path(
    storage: RecordingStorageClient,
    http: TestClient,
) -> None:
    """Reject a relative remote path before calling the storage client."""
    response = post_upload(http, remote_path="relative.pdf")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert storage.uploaded_bytes is None
    assert storage.entries == {}


def test_upload_requires_a_file(storage: RecordingStorageClient, http: TestClient) -> None:
    """Reject an upload request that omits the file field."""
    response = http.post("/files/upload", data={"remote_path": "/Docs/report.pdf"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert storage.uploaded_bytes is None
