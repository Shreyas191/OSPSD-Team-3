"""Fault injection: off by default, refused on demo, and failing the way a real provider does.

The app is started with ``FakeStorageClient`` as its provider and ``FAULT_INJECTION`` set,
so each fault is seen through the real routes, error handlers and status codes.
"""

import importlib
import time
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from tests.fakes import FakeStorageClient

from cloud_storage_service import faults
from cloud_storage_service.app import app

Start = Callable[..., TestClient]


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorageClient:
    """Make the app build a fake holding one file, instead of a Dropbox client."""
    fake = FakeStorageClient()
    fake.add("/Docs/a.txt", size=3)
    monkeypatch.setattr(
        importlib.import_module("cloud_storage_service.app"),
        "build_storage_client",
        lambda: fake,
    )
    for name in (faults.FAULT_ENV_VAR, faults.OPERATIONS_ENV_VAR, faults.RATE_ENV_VAR, "K_SERVICE"):
        monkeypatch.delenv(name, raising=False)
    return fake


def _http(monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[TestClient]:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with TestClient(app) as client:  # runs the lifespan, which applies with_faults
        yield client


@pytest.fixture
def http_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Start]:
    """Return a function that starts the app with the given environment."""
    clients: list[Iterator[TestClient]] = []

    def start(**env: str) -> TestClient:
        gen = _http(monkeypatch, **env)
        clients.append(gen)
        return next(gen)

    yield start
    for gen in clients:
        next(gen, None)


def test_off_by_default_the_client_is_returned_unchanged(storage: FakeStorageClient) -> None:
    """Without FAULT_INJECTION, nothing is wrapped."""
    assert faults.with_faults(storage) is storage


def test_never_enabled_on_the_demo_service(
    storage: FakeStorageClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the demo service a fault setting stops startup instead of injecting anything."""
    monkeypatch.setenv(faults.FAULT_ENV_VAR, "error")
    monkeypatch.setenv("K_SERVICE", faults.DEMO_SERVICE)

    with pytest.raises(faults.FaultConfigError, match="never allowed"):
        faults.with_faults(storage)


@pytest.mark.parametrize(
    ("fault", "operations", "rate", "message"),
    [
        ("slow", "", "", "unknown fault"),
        ("latency", "", "", "needs =<ms>"),
        ("error=100", "", "", "takes no =<ms>"),
        ("latency=abc", "", "", "delay must be"),
        ("latency=999999", "", "", "delay must be"),
        ("error", "get_metadata,teleport", "", "unknown operations"),
        ("error", "", "1.5", "number from 0 to 1"),
        ("error", "", "half", "number from 0 to 1"),
    ],
)
def test_a_malformed_setting_stops_startup(
    fault: str,
    operations: str,
    rate: str,
    message: str,
) -> None:
    """A typo is an error at startup, never a silently ignored setting."""
    with pytest.raises(faults.FaultConfigError, match=message):
        faults.Fault.parse(fault, operations, rate)


@pytest.mark.parametrize(
    ("fault", "status", "code"),
    [("unavailable", 503, "provider_unavailable"), ("error", 502, "provider_error")],
)
def test_failing_faults_look_like_a_failing_provider(
    storage: FakeStorageClient,
    http_factory: Start,
    fault: str,
    status: int,
    code: str,
) -> None:
    """The caller gets the shared error body and the provider-failure status, nothing else."""
    http = http_factory(**{faults.FAULT_ENV_VAR: fault})

    response = http.get("/files/metadata", params={"path": "/Docs/a.txt"})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "Injected" not in response.json()["error"]["message"]


def test_latency_slows_the_answer_but_keeps_it(
    storage: FakeStorageClient,
    http_factory: Start,
) -> None:
    """``latency=200`` waits 200 ms and then answers normally."""
    http = http_factory(**{faults.FAULT_ENV_VAR: "latency=200"})

    started = time.perf_counter()
    response = http.get("/files/metadata", params={"path": "/Docs/a.txt"})

    assert response.status_code == 200
    assert time.perf_counter() - started >= 0.2


def test_timeout_waits_then_reports_the_provider_unavailable(
    storage: FakeStorageClient,
    http_factory: Start,
) -> None:
    """``timeout=200`` waits, then answers 503 as an unreachable provider would."""
    http = http_factory(**{faults.FAULT_ENV_VAR: "timeout=200"})

    started = time.perf_counter()
    response = http.get("/files/metadata", params={"path": "/Docs/a.txt"})

    assert response.status_code == 503
    assert time.perf_counter() - started >= 0.2


def test_lost_response_makes_the_write_but_reports_503(
    storage: FakeStorageClient,
    http_factory: Start,
) -> None:
    """The uncertain write: the caller sees 503, yet the file was created, so a retry is 409."""
    http = http_factory(**{faults.FAULT_ENV_VAR: "lost_response"})

    response = http.post("/files", files={"file": ("b.txt", b"data")}, data={"path": "/Docs/b.txt"})

    assert response.status_code == 503
    assert "/Docs/b.txt" in storage.entries

    # Retrying the "failed" create now conflicts with the file the first attempt made.
    retry = http.post("/files", files={"file": ("b.txt", b"data")}, data={"path": "/Docs/b.txt"})
    assert retry.status_code == 409


def test_only_the_named_operations_fail(storage: FakeStorageClient, http_factory: Start) -> None:
    """With FAULT_OPERATIONS=get_metadata, metadata fails and listing still works."""
    http = http_factory(
        **{faults.FAULT_ENV_VAR: "error", faults.OPERATIONS_ENV_VAR: "get_metadata"},
    )

    assert http.get("/files/metadata", params={"path": "/Docs/a.txt"}).status_code == 502
    assert http.get("/files", params={"path": "/"}).status_code == 200


def test_rate_zero_never_fails(storage: FakeStorageClient, http_factory: Start) -> None:
    """FAULT_RATE=0 injects nothing, so a partial-failure setting can be dialled down to off."""
    http = http_factory(**{faults.FAULT_ENV_VAR: "error", faults.RATE_ENV_VAR: "0"})

    assert http.get("/files/metadata", params={"path": "/Docs/a.txt"}).status_code == 200


def test_every_client_method_goes_through_the_fault(storage: FakeStorageClient) -> None:
    """Each Client method is wrapped, so no operation can bypass an injected fault."""
    client = faults.FaultInjectingClient(storage, faults.Fault("error"))
    calls: dict[str, Callable[[], object]] = {
        "upload_file": lambda: client.upload_file(__file__, "/x"),
        "replace_file": lambda: client.replace_file(__file__, "/Docs/a.txt"),
        "create_folder": lambda: client.create_folder("/New"),
        "copy": lambda: client.copy("/Docs/a.txt", "/b.txt"),
        "download_file": lambda: client.download_file("/Docs/a.txt", "/tmp/never-written"),  # noqa: S108
        "get_metadata": lambda: client.get_metadata("/Docs/a.txt"),
        "list_folder": lambda: list(client.list_folder("/Docs")),
        "search": lambda: list(client.search("a")),
        "rename": lambda: client.rename("/Docs/a.txt", "c.txt"),
        "move": lambda: client.move("/Docs/a.txt", "/c.txt"),
        "check_health": client.check_health,
        "delete": lambda: client.delete("/Docs/a.txt"),
    }
    assert set(calls) == set(faults._OPERATION_NAMES)
    for name, call in calls.items():
        with pytest.raises(OSError, match="Injected"):
            call()
        assert "/Docs/a.txt" in storage.entries, name
