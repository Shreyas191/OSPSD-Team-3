"""HTTP contract tests for ``GET /health`` and ``GET /health/provider``.

Refer to DESIGN.md ("Split health probes"):
- ``GET /health`` answers 200 {"status": "ok"} without calling the provider, so a provider
  outage never restarts the container.
- ``GET /health/provider`` makes one client.check_health() call. It returns 200 when the
  credentials work and 503 when the provider is unreachable or answers with an error.
"""

import logging
from collections.abc import Iterator
from http import HTTPStatus
from unittest.mock import Mock, call

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
    """Install a mock storage client to simulate provider health outcomes."""
    client = Mock(spec=Client)
    client.check_health.return_value = None
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return an HTTP client for the app."""
    return TestClient(app)


# ----- GET /health -----


def test_health_returns_200_without_calling_provider(
    provider: Mock,
    http: TestClient,
) -> None:
    """GET /health answers 200 {"status": "ok"} without contacting the storage provider."""
    response = http.get("/health")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}
    provider.check_health.assert_not_called()


def test_health_returns_200_even_when_provider_is_down(
    provider: Mock,
    http: TestClient,
) -> None:
    """A provider outage never affects GET /health, preventing unnecessary container restarts."""
    provider.check_health.side_effect = ConnectionError("unreachable")

    response = http.get("/health")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}
    provider.check_health.assert_not_called()


# ----- GET /health/provider -----


def test_provider_health_returns_200_when_healthy(
    provider: Mock,
    http: TestClient,
) -> None:
    """GET /health/provider calls client.check_health() and returns 200 {"status": "ok"}."""
    response = http.get("/health/provider")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}
    provider.check_health.assert_called_once_with()


def test_provider_health_with_fake_storage(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """GET /health/provider succeeds with FakeStorageClient when no health_error is set."""
    response = http.get("/health/provider")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}


def test_provider_health_with_fake_storage_error_returns_503(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """GET /health/provider returns 503 when FakeStorageClient has health_error set."""
    storage.health_error = ConnectionError("Dropbox connection failed")

    response = http.get("/health/provider")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.json()
    assert body["error"]["code"] == "provider_unavailable"


def test_provider_health_is_503_when_dropbox_fails(
    provider: Mock,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Return shared 503 provider_unavailable and log a structured warning when unreachable."""
    error = ConnectionError("connection refused to provider")
    provider.check_health.side_effect = error

    with caplog.at_level(logging.WARNING, logger="cloud_storage_service.errors"):
        response = http.get("/health/provider")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.json()["error"]
    assert body["code"] == "provider_unavailable"
    assert body["message"] == "The storage provider is unreachable or busy; try again later"
    assert str(error) not in response.text
    provider.check_health.assert_called_once_with()

    [record] = [r for r in caplog.records if r.name == "cloud_storage_service.errors"]
    assert record.levelno == logging.WARNING
    assert record.__dict__["operation"] == "check_provider_health"
    assert record.__dict__["status"] == HTTPStatus.SERVICE_UNAVAILABLE
    assert record.__dict__["code"] == "provider_unavailable"
    assert record.__dict__["provider_failure"] is True
    assert record.__dict__["reason"] == str(error)


def test_provider_health_when_credentials_invalid_returns_502(
    provider: Mock,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected OSError (e.g., auth error) returns 502 provider_error."""
    error = OSError("Dropbox failed checking the Dropbox account: invalid token")
    provider.check_health.side_effect = error

    with caplog.at_level(logging.WARNING, logger="cloud_storage_service.errors"):
        response = http.get("/health/provider")

    assert response.status_code == HTTPStatus.BAD_GATEWAY
    body = response.json()["error"]
    assert body["code"] == "provider_error"
    assert body["message"] == "The storage provider failed to complete the request"
    assert str(error) not in response.text
    provider.check_health.assert_called_once_with()

    [record] = [r for r in caplog.records if r.name == "cloud_storage_service.errors"]
    assert record.__dict__["operation"] == "check_provider_health"
    assert record.__dict__["status"] == HTTPStatus.BAD_GATEWAY
    assert record.__dict__["code"] == "provider_error"
    assert record.__dict__["provider_failure"] is True
    assert record.__dict__["reason"] == str(error)


def test_provider_health_probes_on_every_request(
    provider: Mock,
    http: TestClient,
) -> None:
    """Each request triggers a fresh probe, detecting transient outages and recovery."""
    provider.check_health.side_effect = [
        None,
        ConnectionError("network blip"),
        None,
    ]

    responses = [http.get("/health/provider") for _ in range(3)]

    assert [r.status_code for r in responses] == [
        HTTPStatus.OK,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.OK,
    ]
    assert provider.check_health.call_args_list == [call(), call(), call()]
