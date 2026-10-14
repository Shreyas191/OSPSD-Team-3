"""HTTP contract tests for the update routes (POST /files/rename and POST /files/move).

The storage client is replaced through FastAPI's dependency overrides with the in-memory
``FakeStorageClient`` from ``tests/fakes.py``, so these tests need no network or
credentials. They check the public behaviour: status codes, response bodies, and what the
storage holds afterwards.
"""

import importlib
from collections.abc import Iterator
from http import HTTPStatus
from unittest.mock import Mock

import pytest
from cloud_storage_client_api import Client
from dropbox_client_impl import DropboxClient
from fastapi.testclient import TestClient
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app, build_storage_client, get_storage_client


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


def test_rename_returns_updated_file(storage: FakeStorageClient, http: TestClient) -> None:
    """A successful rename returns 200 and the file at its new path."""
    storage.add("/Docs/report.pdf", size=2048)

    response = http.post(
        "/files/rename",
        json={"path": "/Docs/report.pdf", "new_name": "summary.pdf"},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": "id:1",
        "name": "summary.pdf",
        "path": "/Docs/summary.pdf",
        "is_folder": False,
        "size": 2048,
        "modified": "2026-10-01T12:30:00",
    }
    assert set(storage.entries) == {"/Docs/summary.pdf"}


def test_move_returns_updated_file(storage: FakeStorageClient, http: TestClient) -> None:
    """A successful move returns 200 and the same entry (same id) at its new path."""
    original = storage.add("/Docs/report.pdf")
    storage.add("/Archive", is_folder=True)

    response = http.post(
        "/files/move",
        json={"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["path"] == "/Archive/report.pdf"
    assert response.json()["id"] == original.id
    assert "/Docs/report.pdf" not in storage.entries


def test_move_folder_returns_folder_and_moves_contents(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Moving a folder returns it with null size and modified, and its contents move too."""
    storage.add("/Docs", is_folder=True)
    storage.add("/Docs/report.pdf")

    response = http.post("/files/move", json={"from_path": "/Docs", "to_path": "/Archive"})

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert (body["is_folder"], body["size"], body["modified"]) == (True, None, None)
    assert set(storage.entries) == {"/Archive", "/Archive/report.pdf"}


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/files/rename", {"path": "Docs/report.pdf", "new_name": "a.pdf"}),
        ("/files/rename", {"path": "/Docs/report.pdf", "new_name": ""}),
        ("/files/rename", {"path": "/Docs/report.pdf", "new_name": "a/b.pdf"}),
        ("/files/rename", {"path": "/Docs/report.pdf"}),
        ("/files/move", {"from_path": "/Docs/report.pdf", "to_path": "Archive"}),
        ("/files/move", {"from_path": "/", "to_path": "/Archive"}),
    ],
)
def test_invalid_input_is_rejected_and_changes_nothing(
    storage: FakeStorageClient,
    http: TestClient,
    route: str,
    body: dict[str, str],
) -> None:
    """Relative paths, empty or nested names and missing fields return 422 and change nothing."""
    storage.add("/Docs/report.pdf")

    response = http.post(route, json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert set(storage.entries) == {"/Docs/report.pdf"}


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/files/rename", {"path": "/Docs/missing.pdf", "new_name": "a.pdf"}),
        ("/files/move", {"from_path": "/Docs/missing.pdf", "to_path": "/Archive/a.pdf"}),
    ],
)
def test_missing_source_returns_404(
    storage: FakeStorageClient,
    http: TestClient,
    route: str,
    body: dict[str, str],
) -> None:
    """Renaming or moving something that does not exist returns 404."""
    response = http.post(route, json=body)

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {"detail": "No file or folder at '/Docs/missing.pdf'"}
    assert storage.entries == {}


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/files/rename", {"path": "/Docs/report.pdf", "new_name": "summary.pdf"}),
        ("/files/move", {"from_path": "/Docs/report.pdf", "to_path": "/Docs/summary.pdf"}),
        ("/files/move", {"from_path": "/Docs/report.pdf", "to_path": "/Docs/report.pdf"}),
    ],
)
def test_occupied_destination_returns_409_and_overwrites_nothing(
    storage: FakeStorageClient,
    http: TestClient,
    route: str,
    body: dict[str, str],
) -> None:
    """A destination that already exists (including the source itself) returns 409."""
    report = storage.add("/Docs/report.pdf")
    summary = storage.add("/Docs/summary.pdf")

    response = http.post(route, json=body)

    assert response.status_code == HTTPStatus.CONFLICT
    assert storage.entries == {"/Docs/report.pdf": report, "/Docs/summary.pdf": summary}


def test_value_error_from_client_returns_422(http: TestClient) -> None:
    """A ``ValueError`` from the client maps to 422.

    Request validation already rejects every name the contract forbids, so the fake can't
    reach this branch through HTTP; a mock raises it directly instead.
    """
    client = Mock(spec=Client)
    client.rename.side_effect = ValueError("Invalid name")
    app.dependency_overrides[get_storage_client] = lambda: client
    try:
        response = http.post(
            "/files/rename",
            json={"path": "/Docs/report.pdf", "new_name": "summary.pdf"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {"detail": "Invalid name"}


def test_routes_use_the_client_built_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without overrides, every request uses the one client the app built at startup."""
    storage = FakeStorageClient()
    storage.add("/Docs/report.pdf")
    storage.add("/Archive", is_folder=True)
    build = Mock(return_value=storage)
    # The package re-exports the FastAPI object as ``cloud_storage_service.app``, so patch
    # the module itself rather than going through that dotted name.
    monkeypatch.setattr(
        importlib.import_module("cloud_storage_service.app"),
        "build_storage_client",
        build,
    )

    with TestClient(app) as http:  # entering the context runs the app's startup
        rename = http.post(
            "/files/rename",
            json={"path": "/Docs/report.pdf", "new_name": "summary.pdf"},
        )
        move = http.post(
            "/files/move",
            json={"from_path": "/Docs/summary.pdf", "to_path": "/Archive/summary.pdf"},
        )

    assert rename.status_code == HTTPStatus.OK
    assert move.status_code == HTTPStatus.OK
    build.assert_called_once_with()
    assert set(storage.entries) == {"/Archive", "/Archive/summary.pdf"}


def test_service_runs_against_dropbox_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider the service selects is Dropbox."""
    monkeypatch.setattr("dropbox_client_impl.dropbox_impl.get_dropbox_client", Mock)

    assert isinstance(build_storage_client(), DropboxClient)
