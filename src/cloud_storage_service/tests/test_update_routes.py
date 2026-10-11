"""HTTP contract tests for the update routes (POST /files/rename and POST /files/move).

The storage client is replaced with a mock through FastAPI's dependency overrides, so
these tests exercise request validation, response shape and status codes only.
"""

import importlib
from collections.abc import Iterator
from datetime import datetime
from http import HTTPStatus
from unittest.mock import Mock

import pytest
from cloud_storage_client_api import Client, File
from dropbox_client_impl import DropboxClient
from fastapi.testclient import TestClient

from cloud_storage_service.app import app, build_storage_client, get_storage_client

MODIFIED = datetime(2026, 10, 1, 12, 30)  # noqa: DTZ001 - providers may return naive UTC datetimes


def _file(path: str) -> Mock:
    file = Mock(spec=File)
    file.id = "id:abc"
    file.path = path
    file.name = path.rsplit("/", 1)[-1]
    file.is_folder = False
    file.size = 2048
    file.modified = MODIFIED
    return file


@pytest.fixture
def storage() -> Iterator[Mock]:
    """Install a mocked storage client for the duration of a test."""
    client = Mock(spec=Client)
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return an HTTP client for the app."""
    return TestClient(app)


def test_rename_returns_updated_file(storage: Mock, http: TestClient) -> None:
    """A successful rename returns 200 and the file at its new path."""
    storage.rename.return_value = _file("/Docs/summary.pdf")

    response = http.post(
        "/files/rename",
        json={"path": "/Docs/report.pdf", "new_name": "summary.pdf"},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": "id:abc",
        "name": "summary.pdf",
        "path": "/Docs/summary.pdf",
        "is_folder": False,
        "size": 2048,
        "modified": "2026-10-01T12:30:00",
    }
    storage.rename.assert_called_once_with("/Docs/report.pdf", "summary.pdf")


def test_move_returns_updated_file(storage: Mock, http: TestClient) -> None:
    """A successful move returns 200 and the file at its new path."""
    storage.move.return_value = _file("/Archive/report.pdf")

    response = http.post(
        "/files/move",
        json={"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json()["path"] == "/Archive/report.pdf"
    storage.move.assert_called_once_with("/Docs/report.pdf", "/Archive/report.pdf")


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
def test_invalid_input_is_rejected_before_reaching_provider(
    storage: Mock,
    http: TestClient,
    route: str,
    body: dict[str, str],
) -> None:
    """Relative paths, empty or nested names and missing fields return 422."""
    response = http.post(route, json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    storage.rename.assert_not_called()
    storage.move.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (FileNotFoundError("No file or folder at '/Docs/report.pdf'"), 404),
        (FileExistsError("Something already exists at '/Archive/report.pdf'"), 409),
    ],
)
def test_move_maps_client_errors_to_status(
    storage: Mock,
    http: TestClient,
    error: Exception,
    expected_status: int,
) -> None:
    """Missing sources and occupied destinations map to 404 and 409."""
    storage.move.side_effect = error

    response = http.post(
        "/files/move",
        json={"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"},
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": str(error)}


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (FileNotFoundError("missing"), 404),
        (FileExistsError("taken"), 409),
        (ValueError("Invalid name"), 422),
    ],
)
def test_rename_maps_client_errors_to_status(
    storage: Mock,
    http: TestClient,
    error: Exception,
    expected_status: int,
) -> None:
    """Client errors on rename map to 404, 409 and 422."""
    storage.rename.side_effect = error

    response = http.post(
        "/files/rename",
        json={"path": "/Docs/report.pdf", "new_name": "summary.pdf"},
    )

    assert response.status_code == expected_status


def test_routes_use_the_client_built_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without overrides, every request uses the one client the app built at startup."""
    client = Mock(spec=Client)
    client.rename.return_value = _file("/Docs/summary.pdf")
    client.move.return_value = _file("/Archive/summary.pdf")
    build = Mock(return_value=client)
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
    client.rename.assert_called_once_with("/Docs/report.pdf", "summary.pdf")
    client.move.assert_called_once_with("/Docs/summary.pdf", "/Archive/summary.pdf")


def test_service_runs_against_dropbox_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider the service selects is Dropbox."""
    monkeypatch.setattr("dropbox_client_impl.dropbox_impl.get_dropbox_client", Mock)

    assert isinstance(build_storage_client(), DropboxClient)
