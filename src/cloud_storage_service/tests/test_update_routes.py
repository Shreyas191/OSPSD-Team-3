"""HTTP contract tests for the update routes (POST /files/rename and POST /files/move).

The storage client is replaced through FastAPI's dependency overrides, either with the
in-memory ``FakeStorageClient`` from ``tests/fakes.py`` or, to simulate provider failures,
a ``Mock(spec=Client)``, so these tests need no network or credentials. They check the
public behaviour: status codes, the shared error body (``{"error": {"code", "message"}}``),
what the storage holds afterwards, and the warning logged for each failure.
"""

import importlib
import logging
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
def provider() -> Iterator[Mock]:
    """Install a mock client whose ``rename``/``move`` a test can make fail like a provider."""
    client = Mock(spec=Client)
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return an HTTP client for the app."""
    return TestClient(app)


RENAME = ("/files/rename", {"path": "/Docs/report.pdf", "new_name": "summary.pdf"})
MOVE = ("/files/move", {"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"})


def _failure_log(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """Return the single warning the service logged for a failed request."""
    [record] = [r for r in caplog.records if r.name == "cloud_storage_service.errors"]
    assert record.levelno == logging.WARNING
    return record


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
    assert response.json()["error"]["code"] == "invalid_input"
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
    assert response.json() == {
        "error": {"code": "not_found", "message": "No file or folder at '/Docs/missing.pdf'"},
    }
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
    assert response.json()["error"]["code"] == "conflict"
    assert storage.entries == {"/Docs/report.pdf": report, "/Docs/summary.pdf": summary}


def test_folder_moved_into_itself_returns_422_and_changes_nothing(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Moving a folder into its own subfolder is invalid input, and nothing moves."""
    storage.add("/Docs", is_folder=True)

    response = http.post("/files/move", json={"from_path": "/Docs", "to_path": "/Docs/Inner"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["error"]["code"] == "invalid_input"
    assert set(storage.entries) == {"/Docs"}


@pytest.mark.parametrize(("route", "body"), [RENAME, MOVE])
def test_value_error_from_client_returns_422(
    provider: Mock,
    http: TestClient,
    route: str,
    body: dict[str, str],
) -> None:
    """A ``ValueError`` from the client (e.g. a path Dropbox rejects as malformed) maps to 422.

    Request validation already rejects the malformed paths it can recognise, so a mock raises
    the error the Dropbox client uses for paths only Dropbox can judge.
    """
    error = ValueError("Cannot move '/Docs/a' to '/Archive/a': a path is invalid")
    provider.rename.side_effect = provider.move.side_effect = error

    response = http.post(route, json=body)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {"error": {"code": "invalid_input", "message": str(error)}}


@pytest.mark.parametrize(("route", "body"), [RENAME, MOVE])
def test_permission_denied_returns_403(
    provider: Mock,
    http: TestClient,
    route: str,
    body: dict[str, str],
) -> None:
    """A ``PermissionError`` from the client is a 403 that keeps the client's message."""
    error = PermissionError("Dropbox does not allow moving '/Docs/report.pdf'")
    provider.rename.side_effect = provider.move.side_effect = error

    response = http.post(route, json=body)

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == {"error": {"code": "permission_denied", "message": str(error)}}


@pytest.mark.parametrize(("route", "body"), [RENAME, MOVE])
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ConnectionError("RateLimitError"), HTTPStatus.SERVICE_UNAVAILABLE, "provider_unavailable"),
        (
            OSError("RelocationError('insufficient_quota', None)"),
            HTTPStatus.BAD_GATEWAY,
            "provider_failure",
        ),
    ],
)
def test_provider_failure_returns_5xx_without_provider_details(  # noqa: PLR0913
    provider: Mock,
    http: TestClient,
    route: str,
    body: dict[str, str],
    error: OSError,
    status: HTTPStatus,
    code: str,
) -> None:
    """Provider failures map to 503 (retry later) or 502, with a fixed message for callers."""
    provider.rename.side_effect = provider.move.side_effect = error

    response = http.post(route, json=body)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert str(error) not in response.json()["error"]["message"]


def test_failure_is_logged_as_a_structured_warning(
    provider: Mock,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider failure logs one warning with the operation, status, code and reason."""
    provider.move.side_effect = OSError("Dropbox failed to move '/Docs/report.pdf' (quota)")

    with caplog.at_level(logging.WARNING):
        http.post(MOVE[0], json=MOVE[1])

    record = _failure_log(caplog)
    assert record.__dict__["operation"] == "move_file"
    assert record.__dict__["status"] == HTTPStatus.BAD_GATEWAY
    assert record.__dict__["code"] == "provider_failure"
    assert record.__dict__["provider_failure"] is True
    assert record.__dict__["reason"] == "Dropbox failed to move '/Docs/report.pdf' (quota)"


def test_conflict_is_logged_as_a_client_error(
    storage: FakeStorageClient,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An occupied destination is logged as a 409 the provider did not cause."""
    storage.add("/Docs/report.pdf")
    storage.add("/Docs/summary.pdf")

    with caplog.at_level(logging.WARNING):
        http.post(RENAME[0], json=RENAME[1])

    record = _failure_log(caplog)
    assert record.__dict__["operation"] == "rename_file"
    assert record.__dict__["status"] == HTTPStatus.CONFLICT
    assert record.__dict__["provider_failure"] is False


def test_unexpected_errors_are_not_disguised_as_provider_failures(
    provider: Mock,
    http: TestClient,
) -> None:
    """An exception outside the contract is a bug: it is not turned into a 4xx or 502/503."""
    provider.move.side_effect = RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        http.post(MOVE[0], json=MOVE[1])


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
