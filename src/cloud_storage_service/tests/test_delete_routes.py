"""HTTP contract tests for the delete route (DELETE /files).

The storage client is replaced through FastAPI's dependency overrides with the in-memory
``FakeStorageClient`` from ``tests/fakes.py``, so these tests need no network or
credentials. They check the public behaviour: status codes, response bodies, and what the
storage holds afterwards.
"""

from collections.abc import Iterator
from http import HTTPStatus
from unittest.mock import Mock

import pytest
from cloud_storage_client_api import Client
from fastapi.testclient import TestClient
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app, get_storage_client


@pytest.fixture
def storage() -> Iterator[FakeStorageClient]:
    """Install an empty fake storage client for the duration of a test."""
    client = FakeStorageClient()
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return an HTTP client for the app."""
    return TestClient(app)


def test_delete_file_returns_204(storage: FakeStorageClient, http: TestClient) -> None:
    """A successful delete returns 204 with an empty body and removes only that file."""
    storage.add("/Docs/report.pdf")
    storage.add("/Docs/notes.txt")

    response = http.delete("/files", params={"path": "/Docs/report.pdf"})

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert response.content == b""
    assert set(storage.entries) == {"/Docs/notes.txt"}


def test_delete_folder_removes_its_contents(storage: FakeStorageClient, http: TestClient) -> None:
    """Deleting a folder removes everything inside it, but not siblings sharing a prefix."""
    storage.add("/Docs", is_folder=True)
    storage.add("/Docs/report.pdf")
    storage.add("/Docs/Old", is_folder=True)
    storage.add("/Docs/Old/draft.txt")
    storage.add("/Docs2/keep.txt")

    response = http.delete("/files", params={"path": "/Docs"})

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert set(storage.entries) == {"/Docs2/keep.txt"}


def test_missing_path_returns_404(storage: FakeStorageClient, http: TestClient) -> None:
    """Deleting something that does not exist returns 404."""
    response = http.delete("/files", params={"path": "/Docs/missing.pdf"})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {"detail": "No file or folder at '/Docs/missing.pdf'"}


def test_repeat_delete_returns_404(storage: FakeStorageClient, http: TestClient) -> None:
    """Deleting the same path twice succeeds the first time and returns 404 the second."""
    storage.add("/Docs/report.pdf")

    first = http.delete("/files", params={"path": "/Docs/report.pdf"})
    second = http.delete("/files", params={"path": "/Docs/report.pdf"})

    assert first.status_code == HTTPStatus.NO_CONTENT
    assert second.status_code == HTTPStatus.NOT_FOUND
    assert storage.entries == {}


@pytest.mark.parametrize(
    "params",
    [
        {"path": "/"},
        {"path": "//"},
        {"path": ""},
        {"path": "Docs/report.pdf"},
        {},
    ],
)
def test_invalid_or_root_path_is_rejected_and_changes_nothing(
    storage: FakeStorageClient,
    http: TestClient,
    params: dict[str, str],
) -> None:
    """Root, empty, relative and missing paths return 422 and leave storage unchanged."""
    report = storage.add("/Docs/report.pdf")

    response = http.delete("/files", params=params)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert storage.entries == {"/Docs/report.pdf": report}


@pytest.mark.parametrize("path", ["/", ""])
def test_root_path_never_reaches_the_client(http: TestClient, path: str) -> None:
    """Request validation rejects the root before the provider's ``delete`` is called."""
    client = Mock(spec=Client)
    app.dependency_overrides[get_storage_client] = lambda: client
    try:
        response = http.delete("/files", params={"path": path})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    client.delete.assert_not_called()
