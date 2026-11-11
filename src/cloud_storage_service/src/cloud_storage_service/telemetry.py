"""Telemetry: one structured log line per request, and traces of the service and its Dropbox calls.

**Logs.** :class:`RequestTelemetryMiddleware` writes exactly one record per HTTP request to
the ``cloud_storage_service.requests`` logger: the operation, the outcome (``ok`` or the
error ``code``), status, duration, the release, and the trace ID. Failures add
``provider_failure`` and ``reason``, which the error handlers record with
:func:`record_failure`. With ``LOG_FORMAT=json`` (set on Cloud Run) records are printed as
JSON, which Cloud Logging parses and links to the request's trace.

**Traces.** With ``TRACE_EXPORTER=gcp`` (set on Cloud Run) OpenTelemetry traces every
request and, as separate child spans, every HTTP call the Dropbox SDK makes, and exports
them to Cloud Trace. Spans are exported in the background with a bounded queue: if Cloud
Trace is slow or down, spans are dropped and requests are not affected.

**What is never recorded:** request or response bodies, file contents, file paths (quoted
text in error reasons is redacted), query strings, headers (so no tokens), and the values a
caller sent that failed validation.
"""

import json
import logging
import os
import re
import sys
import time
from contextvars import ContextVar
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_logger = logging.getLogger("cloud_storage_service.requests")

RELEASE = os.environ.get("RELEASE", "unknown")
SERVICE = os.environ.get("K_SERVICE", "cloud-storage-service")

# The failure details of the request being handled, filled in by ``record_failure``.
_failure: ContextVar[dict[str, Any] | None] = ContextVar("request_failure", default=None)

# Error messages quote the paths they are about ("No file or folder at '/Docs/a.pdf'").
# File names are user data, so quoted text is replaced before a reason is logged.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")

HTTP_CLIENT_ERROR = 400
HTTP_SERVER_ERROR = 500


def record_failure(
    operation: str,
    status_code: int,
    code: str,
    reason: str,
    *,
    provider_failure: bool | None = None,
) -> None:
    """Attach a failure's details to the current request's log line.

    ``provider_failure`` defaults to "the status is 502 or 503"; callers that answer 503
    without asking the provider (the service shedding load) pass ``False``. Outside a
    request (no middleware), the failure is logged on its own.
    """
    if provider_failure is None:
        provider_failure = status_code in {502, 503}
    fields = {
        "operation": operation,
        "status": status_code,
        "code": code,
        "provider_failure": provider_failure,
        "reason": reason,
    }
    fields["reason"] = redact(reason)
    current = _failure.get()
    if current is None:
        request_logger.warning("Request failed: %s", _pairs(fields), extra=fields)
    else:
        current.update(fields)


def redact(text: str) -> str:
    """Return ``text`` with every quoted part replaced by ``'…'``, so no path is logged."""
    return _QUOTED.sub("'…'", text)


def _pairs(fields: dict[str, Any]) -> str:
    return " ".join(f"{key}={value!r}" for key, value in fields.items())


class RequestTelemetryMiddleware:
    """ASGI middleware that writes the one log record of every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the request and log its outcome, even if it raised."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        failure: dict[str, Any] = {}
        token = _failure.set(failure)
        started = time.perf_counter()
        status_code = HTTP_SERVER_ERROR

        async def send_and_note_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_note_status)
        except Exception as exc:
            # The app's 500 handler answers after this middleware, so record it here.
            failure.setdefault("code", "internal_error")
            failure.setdefault("reason", redact(repr(exc)))
            failure.setdefault("provider_failure", False)
            status_code = HTTP_SERVER_ERROR
            raise
        finally:
            _failure.reset(token)
            _log_request(scope, status_code, failure, time.perf_counter() - started)


def _log_request(
    scope: Scope,
    status_code: int,
    failure: dict[str, Any],
    seconds: float,
) -> None:
    route = scope.get("route")
    route_name = getattr(route, "name", None)
    fields: dict[str, Any] = {
        "event": "request",
        # Bounded, so it can be a metric label: the route's name, never the raw path.
        "route": route_name or "unmatched",
        "operation": failure.get("operation") or route_name or f"{scope['method']} {scope['path']}",
        "method": scope["method"],
        "status": status_code,
        "outcome": failure.get("code", "ok" if status_code < HTTP_CLIENT_ERROR else "error"),
        "duration_ms": round(seconds * 1000, 1),
        "release": RELEASE,
        "service": SERVICE,
    }
    if failure:
        fields["code"] = fields["outcome"]
        fields["provider_failure"] = failure.get("provider_failure", False)
        fields["reason"] = failure.get("reason", "")
    fields.update(_trace_fields())

    if status_code >= HTTP_SERVER_ERROR:
        level = logging.ERROR
    elif status_code >= HTTP_CLIENT_ERROR:
        level = logging.WARNING
    else:
        level = logging.INFO
    if failure:
        summary = {
            "operation": fields["operation"],
            "status": status_code,
            "code": fields["outcome"],
            "provider_failure": fields["provider_failure"],
        }
        request_logger.log(level, "Request failed: %s", _pairs(summary), extra=fields)
    else:
        request_logger.log(
            level,
            "%s %s %s",
            fields["operation"],
            status_code,
            fields["outcome"],
            extra=fields,
        )


def _trace_fields() -> dict[str, str]:
    """Return the current trace and span IDs, in the fields Cloud Logging links to traces."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    trace_id = format(context.trace_id, "032x")
    fields = {"trace_id": trace_id, "span_id": format(context.span_id, "016x")}
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project:
        fields["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace_id}"
        fields["logging.googleapis.com/spanId"] = fields["span_id"]
    return fields


# ----- Log format -----

# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Format records as one JSON object per line, the way Cloud Logging reads them."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as JSON: severity, message, logger and every ``extra`` field."""
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Send logs to stdout, as JSON when ``LOG_FORMAT=json`` (Cloud Run), else as text."""
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("LOG_FORMAT") == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


# ----- Traces -----


def configure_tracing(app: FastAPI) -> None:
    """Trace requests and Dropbox calls to Cloud Trace when ``TRACE_EXPORTER=gcp``."""
    if os.environ.get("TRACE_EXPORTER") != "gcp":
        return
    # Imported here so tests and local runs never load the exporter or touch Google Cloud.
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter  # noqa: PLC0415
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.requests import RequestsInstrumentor  # noqa: PLC0415
    from opentelemetry.propagate import set_global_textmap  # noqa: PLC0415
    from opentelemetry.propagators.cloud_trace_propagator import (  # noqa: PLC0415
        CloudTraceFormatPropagator,
    )
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE, "service.version": RELEASE}),
    )
    # Bounded queue, exported in the background: if Cloud Trace is unavailable, spans are
    # dropped instead of slowing or failing requests.
    # The exporter package has no type hints.
    provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter(), max_queue_size=2048))  # type: ignore[no-untyped-call]
    trace.set_tracer_provider(provider)
    # Continue the trace Cloud Run starts for each request (X-Cloud-Trace-Context).
    set_global_textmap(CloudTraceFormatPropagator())
    # The health probes run every 30 s; tracing them would only add noise.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
    # Every Dropbox API call goes through `requests`, so each one becomes its own span.
    RequestsInstrumentor().instrument()
