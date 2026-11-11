"""Telemetry: one structured log record per request, in JSON that Cloud Logging links to traces.

Requests go through the real app with ``FakeStorageClient``. No network, no credentials,
and no trace exporter: tracing is only switched on with ``TRACE_EXPORTER=gcp``.
"""

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from tests.fakes import FakeStorageClient

from cloud_storage_service import telemetry
from cloud_storage_service.app import app, get_storage_client


@pytest.fixture
def storage() -> Iterator[FakeStorageClient]:
    """Install a fake holding one file."""
    fake = FakeStorageClient()
    fake.add("/Docs/secret-plan.pdf", size=10)
    app.dependency_overrides[get_storage_client] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return an HTTP client for the app."""
    return TestClient(app)


def _request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "cloud_storage_service.requests"]


def test_a_successful_request_logs_one_record_with_the_operation_and_outcome(
    storage: FakeStorageClient,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 200 writes exactly one INFO record with what an engineer needs to explain it."""
    with caplog.at_level(logging.INFO):
        http.get("/files/metadata", params={"path": "/Docs/secret-plan.pdf"})

    [record] = _request_records(caplog)
    fields = record.__dict__
    assert record.levelno == logging.INFO
    assert fields["event"] == "request"
    assert fields["operation"] == "get_file_metadata"
    assert fields["route"] == "get_file_metadata"
    assert fields["method"] == "GET"
    assert fields["status"] == 200
    assert fields["outcome"] == "ok"
    assert fields["release"] == telemetry.RELEASE
    assert isinstance(fields["duration_ms"], float)


def test_a_failed_request_logs_one_record_not_two(
    storage: FakeStorageClient,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 404 is one WARNING record carrying the error code and reason."""
    with caplog.at_level(logging.INFO):
        http.get("/files/metadata", params={"path": "/Docs/missing.pdf"})

    [record] = _request_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.__dict__["outcome"] == "not_found"
    assert record.__dict__["code"] == "not_found"
    assert record.__dict__["provider_failure"] is False


def test_an_unexpected_exception_is_logged_once_as_an_error(
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bug is one ERROR record with the exception, and the caller sees a fixed message."""

    class Broken(FakeStorageClient):
        def get_metadata(self, remote_path: str) -> object:  # type: ignore[override]
            msg = "boom"
            raise RuntimeError(msg)

    app.dependency_overrides[get_storage_client] = Broken
    try:
        with caplog.at_level(logging.INFO):
            TestClient(app, raise_server_exceptions=False).get(
                "/files/metadata",
                params={"path": "/a"},
            )
    finally:
        app.dependency_overrides.clear()

    [record] = _request_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.__dict__["outcome"] == "internal_error"
    assert record.__dict__["reason"] == "RuntimeError('…')"


def test_failure_reasons_are_logged_without_paths(
    storage: FakeStorageClient,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The caller's 404 message names the path; the log line's reason does not."""
    with caplog.at_level(logging.INFO):
        response = http.get("/files/metadata", params={"path": "/Docs/private-salary.xlsx"})

    [record] = _request_records(caplog)
    assert "private-salary" in response.json()["error"]["message"]
    assert "private-salary" not in telemetry.JsonFormatter().format(record)
    assert record.__dict__["reason"] == "No file or folder at '…'"


def test_logs_never_contain_the_path_or_query(
    storage: FakeStorageClient,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """File paths are user data: the record names the route, not the path or query string."""
    with caplog.at_level(logging.INFO):
        http.get("/files/metadata", params={"path": "/Docs/secret-plan.pdf"})

    [record] = _request_records(caplog)
    line = telemetry.JsonFormatter().format(record)
    assert "secret-plan" not in line
    assert "Docs" not in line


def test_json_lines_carry_severity_fields_and_the_trace_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a span, the record has the trace ID in the field Cloud Logging links to traces."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("request") as span:
        fields = telemetry._trace_fields()
        trace_id = format(span.get_span_context().trace_id, "032x")
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "Request failed", None, None)
    record.__dict__.update({"event": "request", "status": 503, **fields})

    payload = json.loads(telemetry.JsonFormatter().format(record))

    assert payload["severity"] == "WARNING"
    assert payload["message"] == "Request failed"
    assert payload["status"] == 503
    assert payload["trace_id"] == trace_id
    assert payload["logging.googleapis.com/trace"] == f"projects/demo-project/traces/{trace_id}"


def test_no_span_means_no_trace_fields() -> None:
    """Outside a span (tracing off), the record simply has no trace fields."""
    assert telemetry._trace_fields() == {}


def test_tracing_stays_off_unless_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without TRACE_EXPORTER=gcp nothing is exported, so tests and local runs never call Google."""
    monkeypatch.delenv("TRACE_EXPORTER", raising=False)
    before = trace.get_tracer_provider()

    telemetry.configure_tracing(app)

    assert trace.get_tracer_provider() is before


def test_failures_outside_a_request_are_still_logged(caplog: pytest.LogCaptureFixture) -> None:
    """``record_failure`` with no request in progress logs the failure on its own."""
    with caplog.at_level(logging.WARNING):
        telemetry.record_failure("startup", 503, "provider_unavailable", "Dropbox unreachable")

    [record] = _request_records(caplog)
    assert record.__dict__["code"] == "provider_unavailable"
    assert record.__dict__["provider_failure"] is True


@pytest.mark.parametrize(("log_format", "starts_with"), [("json", "{"), ("text", "INFO")])
def test_configure_logging_picks_the_format(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    log_format: str,
    starts_with: str,
) -> None:
    """LOG_FORMAT=json (Cloud Run) prints JSON lines; otherwise plain text."""
    monkeypatch.setenv("LOG_FORMAT", log_format)
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        telemetry.configure_logging()
        logging.getLogger("x").info("hello")
    finally:
        root.handlers, root.level = saved_handlers, saved_level

    assert capsys.readouterr().out.startswith(starts_with)


def test_request_spans_lose_the_query_string() -> None:
    """A path in ``?path=`` is user data, so the span keeps only the URL's path."""
    span = (
        TracerProvider()
        .get_tracer("test")
        .start_span(
            "GET /files/metadata",
            attributes={
                "http.url": "https://svc.run.app/files/metadata?path=/Docs/private.pdf",
                "http.target": "/files/metadata?path=/Docs/private.pdf",
            },
        )
    )

    telemetry.strip_query(span, {"path": "/files/metadata"})

    attributes = dict(getattr(span, "attributes", None) or {})
    assert attributes["http.url"] == "https://svc.run.app/files/metadata"
    assert attributes["http.target"] == "/files/metadata"
    assert "private" not in str(attributes)
