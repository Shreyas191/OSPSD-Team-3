"""HTTP contract tests for ``GET /health/provider``.

The storage client is replaced through ``app.dependency_overrides`` with a mock
that follows the client interface. The tests verify that each request runs a
fresh probe, that success returns ``200``, and that connection failures return
the shared ``503`` error body and log a structured warning.
"""

import logging
from collections.abc import Iterator
from unittest.mock import Mock, call

import cloud_storage_client_api
import pytest
from fastapi import status
from fastapi.testclient import TestClient

from cloud_storage_service.app import app, get_storage_client

HEALTH_PATH = "/health/provider"


@pytest.fixture
def provider() -> Iterator[Mock]:
    """Install a mock storage client for the duration of a test."""
    client = Mock(spec=cloud_storage_client_api.Client)
    client.check_provider_health.return_value = None
    app.dependency_overrides[get_storage_client] = lambda: client
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_storage_client, None)


@pytest.fixture
def http() -> TestClient:
    """Create a test client without starting the real Dropbox lifespan."""
    return TestClient(app)


def test_deep_health_returns_200(provider: Mock, http: TestClient) -> None:
    """Return 200 after the provider probe succeeds."""
    response = http.get(HEALTH_PATH)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}
    provider.check_provider_health.assert_called_once_with()


def test_deep_health_returns_503_and_logs_failure(
    provider: Mock,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Return a shared 503 and keep the provider diagnostic in the warning log."""
    error = ConnectionError("upstream diagnostic detail")
    provider.check_provider_health.side_effect = error

    with caplog.at_level(logging.WARNING, logger="cloud_storage_service.errors"):
        response = http.get(HEALTH_PATH)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    body = response.json()["error"]
    assert body["code"] == "provider_unavailable"
    assert body["message"]
    assert str(error) not in response.text
    provider.check_provider_health.assert_called_once_with()

    [record] = [
        record for record in caplog.records if record.name == "cloud_storage_service.errors"
    ]
    assert record.levelno == logging.WARNING
    assert record.__dict__["operation"] == "get_provider_health"
    assert record.__dict__["status"] == status.HTTP_503_SERVICE_UNAVAILABLE
    assert record.__dict__["code"] == "provider_unavailable"
    assert record.__dict__["provider_failure"] is True
    assert record.__dict__["reason"] == str(error)


def test_deep_health_probes_on_every_request(provider: Mock, http: TestClient) -> None:
    """Detect an outage and subsequent recovery on consecutive requests."""
    provider.check_provider_health.side_effect = [
        None,
        ConnectionError("provider unavailable"),
        None,
    ]

    responses = [http.get(HEALTH_PATH) for _ in range(3)]

    assert [response.status_code for response in responses] == [
        status.HTTP_200_OK,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        status.HTTP_200_OK,
    ]
    assert provider.check_provider_health.call_args_list == [call(), call(), call()]
