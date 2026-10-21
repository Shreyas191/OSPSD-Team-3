"""Tests for overload protection (``ConcurrencyLimitMiddleware``).

Requests go through the real app and routes. The storage client is a fake whose
``rename`` blocks until the test releases it, so the test decides exactly how many
requests are in flight; nothing depends on timing or sleeps. No network or credentials.
"""

import importlib
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from http import HTTPStatus
from typing import cast

import pytest
from cloud_storage_client_api import File
from fastapi.testclient import TestClient
from httpx import Response
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app
from cloud_storage_service.overload import (
    DEFAULT_MAX_IN_FLIGHT,
    MAX_IN_FLIGHT_ENV_VAR,
    ConcurrencyLimitMiddleware,
    max_in_flight_from_env,
)

WAIT = 5  # seconds; only reached if the code under test is broken
LIMIT = 2


class BlockingStorageClient(FakeStorageClient):
    """A fake whose ``rename`` waits until the test calls :meth:`release`."""

    def __init__(self) -> None:
        """Start empty, with no rename running."""
        super().__init__()
        self.started = threading.Semaphore(0)
        self._release = threading.Event()
        self.rename_calls = 0

    def rename(self, remote_path: str, new_name: str) -> File:
        """Signal that a rename started, then block until released."""
        self.rename_calls += 1
        self.started.release()
        if not self._release.wait(WAIT):
            msg = "test never released the blocked rename"
            raise RuntimeError(msg)
        return super().rename(remote_path, new_name)

    def release(self) -> None:
        """Let every blocked and future rename finish."""
        self._release.set()


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> BlockingStorageClient:
    """Make the app build the blocking fake at startup instead of a Dropbox client."""
    client = BlockingStorageClient()
    for name in ("a", "b", "c", "d"):
        client.add(f"/{name}.txt")
    monkeypatch.setattr(
        importlib.import_module("cloud_storage_service.app"),
        "build_storage_client",
        lambda: client,
    )
    return client


@pytest.fixture
def http(storage: BlockingStorageClient) -> Iterator[TestClient]:
    """Return an HTTP client for the app behind a limit of ``LIMIT`` requests in flight."""
    limited = ConcurrencyLimitMiddleware(app, max_in_flight=LIMIT)
    # Used as a context manager so every request shares one event loop, like a real server.
    with TestClient(limited) as client:
        yield client
    storage.release()


def _rename(http: TestClient, name: str) -> Response:
    body = {"path": f"/{name}.txt", "new_name": f"{name}2.txt"}
    return cast("Response", http.post("/files/rename", json=body))


def _fill_every_slot(
    http: TestClient,
    storage: BlockingStorageClient,
    pool: ThreadPoolExecutor,
) -> list[Future[Response]]:
    """Start ``LIMIT`` renames and wait until each one is blocked inside the client."""
    running = [pool.submit(_rename, http, name) for name in ("a", "b")]
    for _ in running:
        assert storage.started.acquire(timeout=WAIT), "a request never reached the client"
    return running


def test_request_over_the_limit_gets_503_without_reaching_the_provider(
    http: TestClient,
    storage: BlockingStorageClient,
) -> None:
    """With every slot taken, the next request is rejected at once and the client is not called."""
    with ThreadPoolExecutor(max_workers=LIMIT) as pool:
        running = _fill_every_slot(http, storage, pool)

        rejected = _rename(http, "c")

        assert rejected.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert rejected.json()["error"]["code"] == "service_busy"
        assert rejected.headers["Retry-After"] == "1"
        assert storage.rename_calls == LIMIT

        storage.release()
        assert [f.result(WAIT).status_code for f in running] == [HTTPStatus.OK] * LIMIT


def test_requests_are_accepted_again_once_a_slot_frees_up(
    http: TestClient,
    storage: BlockingStorageClient,
) -> None:
    """The limit bounds concurrency, not throughput: after the running requests finish, new ones succeed."""
    with ThreadPoolExecutor(max_workers=LIMIT) as pool:
        running = _fill_every_slot(http, storage, pool)
        storage.release()
        for future in running:
            future.result(WAIT)

    assert _rename(http, "c").status_code == HTTPStatus.OK
    assert _rename(http, "d").status_code == HTTPStatus.OK


def test_failed_requests_free_their_slot(http: TestClient, storage: BlockingStorageClient) -> None:
    """A request that fails (here, a 404) releases its slot like a successful one."""
    storage.release()
    for _ in range(LIMIT + 1):
        missing = http.post("/files/rename", json={"path": "/missing.txt", "new_name": "x.txt"})
        assert missing.status_code == HTTPStatus.NOT_FOUND

    assert _rename(http, "a").status_code == HTTPStatus.OK


def test_api_docs_stay_available_when_the_service_is_full(
    http: TestClient,
    storage: BlockingStorageClient,
) -> None:
    """Swagger UI and the OpenAPI schema are not counted, so they load under load."""
    with ThreadPoolExecutor(max_workers=LIMIT) as pool:
        running = _fill_every_slot(http, storage, pool)

        assert http.get("/openapi.json").status_code == HTTPStatus.OK
        assert http.get("/docs").status_code == HTTPStatus.OK

        storage.release()
        for future in running:
            future.result(WAIT)


def test_rejection_is_logged_as_a_service_failure_not_a_provider_failure(
    http: TestClient,
    storage: BlockingStorageClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A shed request logs one warning that says the provider was not involved."""
    with ThreadPoolExecutor(max_workers=LIMIT) as pool:
        running = _fill_every_slot(http, storage, pool)
        with caplog.at_level(logging.WARNING):
            _rename(http, "c")
        storage.release()
        for future in running:
            future.result(WAIT)

    [record] = [r for r in caplog.records if r.name == "cloud_storage_service.errors"]
    assert record.__dict__["operation"] == "POST /files/rename"
    assert record.__dict__["status"] == HTTPStatus.SERVICE_UNAVAILABLE
    assert record.__dict__["code"] == "service_busy"
    assert record.__dict__["provider_failure"] is False
    assert record.__dict__["reason"] == f"{LIMIT} requests in flight (limit {LIMIT})"


def test_the_app_is_protected_with_the_configured_limit() -> None:
    """The service app itself installs the middleware with the limit from the environment."""
    [middleware] = app.user_middleware
    assert cast("object", middleware.cls) is ConcurrencyLimitMiddleware
    assert middleware.kwargs == {"max_in_flight": max_in_flight_from_env()}


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", DEFAULT_MAX_IN_FLIGHT), ("4", 4), (" 25 ", 25)],
)
def test_limit_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: int,
) -> None:
    """``MAX_IN_FLIGHT_REQUESTS`` sets the limit; unset or empty means the default."""
    monkeypatch.setenv(MAX_IN_FLIGHT_ENV_VAR, value)

    assert max_in_flight_from_env() == expected


@pytest.mark.parametrize("value", ["0", "-1", "ten", "2.5"])
def test_invalid_limit_stops_the_service_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """A limit that isn't a positive whole number is an error, not a silently disabled limit."""
    monkeypatch.setenv(MAX_IN_FLIGHT_ENV_VAR, value)

    with pytest.raises(ValueError, match=MAX_IN_FLIGHT_ENV_VAR):
        max_in_flight_from_env()


def test_limit_below_one_is_rejected() -> None:
    """The middleware itself refuses a limit that would reject every request."""
    with pytest.raises(ValueError, match="at least 1"):
        ConcurrencyLimitMiddleware(app, max_in_flight=0)
