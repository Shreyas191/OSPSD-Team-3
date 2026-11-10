"""HTTP contract tests for the app-level error handlers in ``errors.py``.

Requests that never reach a route (an unknown path, a method the route does not allow)
and unexpected exceptions (bugs) still answer with the shared error body
``{"error": {"code", "message"}}``: 404 ``route_not_found``, 405 ``method_not_allowed``
and 500 ``internal_error``. The storage client is the in-memory ``FakeStorageClient``, so
no network or credentials are needed.
"""

import logging
from collections.abc import Iterator

import pytest
from cloud_storage_client_api import File
from fastapi import status
from fastapi.testclient import TestClient
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app, get_storage_client


class BrokenStorageClient(FakeStorageClient):
    """A fake whose ``get_metadata`` fails with an exception outside the ``Client`` contract."""

    def get_metadata(self, remote_path: str) -> File:
        """Raise a ``RuntimeError``, as a bug in a client would."""
        msg = f"secret internal detail for {remote_path}"
        raise RuntimeError(msg)


@pytest.fixture
def storage() -> Iterator[FakeStorageClient]:
    """Install an empty fake storage client for the duration of a test."""
    client = FakeStorageClient()
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def broken() -> Iterator[BrokenStorageClient]:
    """Install a fake storage client whose ``get_metadata`` raises ``RuntimeError``."""
    client = BrokenStorageClient()
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


def test_unknown_route_returns_404_route_not_found(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """A path no route matches returns 404 ``route_not_found`` in the shared error body."""
    response = http.get("/no/such/route")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {
        "error": {"code": "route_not_found", "message": "No route /no/such/route"},
    }


def test_unknown_route_is_logged_as_a_client_error(
    storage: FakeStorageClient,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown route logs one warning that is not a provider failure."""
    with caplog.at_level(logging.WARNING):
        http.get("/no/such/route")

    record = _failure_log(caplog)
    assert record.__dict__["operation"] == "GET /no/such/route"
    assert record.__dict__["status"] == status.HTTP_404_NOT_FOUND
    assert record.__dict__["code"] == "route_not_found"
    assert record.__dict__["provider_failure"] is False


def test_wrong_method_returns_405_with_allow_header(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """A method the route does not allow returns 405 and keeps the ``Allow`` header."""
    response = http.post("/files/metadata", params={"path": "/Docs/a.txt"})

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert response.json() == {
        "error": {
            "code": "method_not_allowed",
            "message": "POST is not allowed on /files/metadata",
        },
    }
    assert "GET" in response.headers["allow"]


def test_unexpected_exception_returns_500_with_a_fixed_message(
    broken: BrokenStorageClient,
) -> None:
    """An exception outside the contract is a 500 ``internal_error`` that hides its text."""
    http = TestClient(app, raise_server_exceptions=False)

    response = http.get("/files/metadata", params={"path": "/Docs/a.txt"})

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {
        "error": {"code": "internal_error", "message": "The service failed unexpectedly"},
    }
    assert "secret internal detail" not in response.text


def test_unexpected_exception_is_logged_but_not_as_a_provider_failure(
    broken: BrokenStorageClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 500 keeps the exception's text in the log only, and is not blamed on the provider."""
    http = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.WARNING):
        http.get("/files/metadata", params={"path": "/Docs/a.txt"})

    record = _failure_log(caplog)
    assert record.__dict__["status"] == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert record.__dict__["code"] == "internal_error"
    assert record.__dict__["provider_failure"] is False
    assert "secret internal detail" in record.__dict__["reason"]


def test_body_that_is_not_json_returns_400(storage: FakeStorageClient, http: TestClient) -> None:
    """A JSON route given a body that is not JSON returns 400 ``invalid_request``."""
    storage.add("/Docs/a.txt")

    response = http.post(
        "/files/move",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"
    assert set(storage.entries) == {"/Docs/a.txt"}
