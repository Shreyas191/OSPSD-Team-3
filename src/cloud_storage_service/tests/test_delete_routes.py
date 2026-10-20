"""HTTP contract tests for the delete route (DELETE /files).

The storage client is replaced through FastAPI's dependency overrides, either with the
in-memory ``FakeStorageClient`` from ``tests/fakes.py`` or, to simulate provider failures,
a ``Mock(spec=Client)``, so these tests need no network or credentials. They check the
public behaviour: status codes, the shared error body, what the storage holds afterwards,
and the warning logged for each failure.
"""

import logging
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
def provider() -> Iterator[Mock]:
    """Install a mock client whose ``delete`` a test can make fail like a provider would."""
    client = Mock(spec=Client)
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return an HTTP client for the app."""
    return TestClient(app)


def _failure_log(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """Return the single warning the service logged for a failed request."""
    [record] = [r for r in caplog.records if r.name == "cloud_storage_service.errors"]
    assert record.levelno == logging.WARNING
    return record


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
    """Deleting something that does not exist returns 404 in the shared error format."""
    response = http.delete("/files", params={"path": "/Docs/missing.pdf"})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {
        "error": {"code": "not_found", "message": "No file or folder at '/Docs/missing.pdf'"},
    }


def test_repeat_delete_returns_404(storage: FakeStorageClient, http: TestClient) -> None:
    """Deleting the same path twice succeeds the first time and returns 404 the second."""
    storage.add("/Docs/report.pdf")

    first = http.delete("/files", params={"path": "/Docs/report.pdf"})
    second = http.delete("/files", params={"path": "/Docs/report.pdf"})

    assert first.status_code == HTTPStatus.NO_CONTENT
    assert second.status_code == HTTPStatus.NOT_FOUND
    assert second.json()["error"]["code"] == "not_found"
    assert storage.entries == {}


@pytest.mark.parametrize(
    "params",
    [
        {"path": "/"},
        {"path": "//"},
        {"path": ""},
        {"path": "Docs/report.pdf"},
        {"path": "/Docs/"},
        {},
    ],
)
def test_invalid_or_root_path_is_rejected_and_changes_nothing(
    storage: FakeStorageClient,
    http: TestClient,
    params: dict[str, str],
) -> None:
    """Root, empty, relative, trailing-slash and missing paths return 422 and change nothing."""
    report = storage.add("/Docs/report.pdf")

    response = http.delete("/files", params=params)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    error = response.json()["error"]
    assert error["code"] == "invalid_input"
    assert error["message"].startswith("query.path: ")
    assert storage.entries == {"/Docs/report.pdf": report}


@pytest.mark.parametrize("path", ["/", ""])
def test_root_path_never_reaches_the_client(provider: Mock, http: TestClient, path: str) -> None:
    """Request validation rejects the root before the provider's ``delete`` is called."""
    response = http.delete("/files", params={"path": path})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["error"]["code"] == "invalid_input"
    provider.delete.assert_not_called()


def test_path_rejected_by_the_client_returns_422(provider: Mock, http: TestClient) -> None:
    """A ``ValueError`` from the client (e.g. the provider finds the path malformed) is a 422."""
    provider.delete.side_effect = ValueError("Dropbox rejected '/Docs/bad' as an invalid path")

    response = http.delete("/files", params={"path": "/Docs/bad"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {
        "error": {
            "code": "invalid_input",
            "message": "Dropbox rejected '/Docs/bad' as an invalid path",
        },
    }


def test_permission_denied_returns_403(provider: Mock, http: TestClient) -> None:
    """A ``PermissionError`` from the client is a 403 that keeps the client's message."""
    provider.delete.side_effect = PermissionError("Dropbox does not allow deleting '/Team'")

    response = http.delete("/files", params={"path": "/Team"})

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == {
        "error": {
            "code": "permission_denied",
            "message": "Dropbox does not allow deleting '/Team'",
        },
    }


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            ConnectionError("too_many_write_operations"),
            HTTPStatus.SERVICE_UNAVAILABLE,
            "provider_unavailable",
        ),
        (
            OSError("DeleteError('too_many_files', None)"),
            HTTPStatus.BAD_GATEWAY,
            "provider_failure",
        ),
    ],
)
def test_provider_failure_returns_5xx_without_provider_details(
    provider: Mock,
    http: TestClient,
    error: OSError,
    status: HTTPStatus,
    code: str,
) -> None:
    """Provider failures map to 503 (retry later) or 502, with a fixed message for callers."""
    provider.delete.side_effect = error

    response = http.delete("/files", params={"path": "/Docs/report.pdf"})

    assert response.status_code == status
    body = response.json()
    assert body["error"]["code"] == code
    assert str(error) not in body["error"]["message"]


def test_failure_is_logged_as_a_structured_warning(
    provider: Mock,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider failure logs one warning with the operation, status, code and reason."""
    provider.delete.side_effect = OSError(
        "Dropbox failed to delete '/Docs/report.pdf' (InternalServerError)",
    )

    with caplog.at_level(logging.WARNING):
        http.delete("/files", params={"path": "/Docs/report.pdf"})

    record = _failure_log(caplog)
    assert record.__dict__["operation"] == "delete_entry"
    assert record.__dict__["status"] == HTTPStatus.BAD_GATEWAY
    assert record.__dict__["code"] == "provider_failure"
    assert record.__dict__["provider_failure"] is True
    assert (
        record.__dict__["reason"]
        == "Dropbox failed to delete '/Docs/report.pdf' (InternalServerError)"
    )
    assert "status=502 code='provider_failure' provider_failure=True" in record.getMessage()


def test_validation_failure_log_omits_the_rejected_input(
    storage: FakeStorageClient,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid input is logged as a client error, describing the problem but not the value sent."""
    with caplog.at_level(logging.WARNING):
        http.delete("/files", params={"path": "relative/secret-name.txt"})

    record = _failure_log(caplog)
    assert record.__dict__["status"] == HTTPStatus.UNPROCESSABLE_ENTITY
    assert record.__dict__["provider_failure"] is False
    assert "secret-name" not in record.getMessage()


def test_unexpected_errors_are_not_disguised_as_provider_failures(
    provider: Mock,
    http: TestClient,
) -> None:
    """An exception outside the contract is a bug: it is not turned into a 4xx or 502/503."""
    provider.delete.side_effect = RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        http.delete("/files", params={"path": "/Docs/report.pdf"})
