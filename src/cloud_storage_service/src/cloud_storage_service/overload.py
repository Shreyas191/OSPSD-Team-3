"""Overload protection: a bound on how many requests the service works on at once.

Routes are synchronous, so FastAPI runs each request on a worker thread (40 by default).
Without a bound, a burst beyond that waits in memory for a thread, with no limit on how
many requests pile up or how long they wait, and every one of them eventually calls
Dropbox, which rate-limits bursts anyway.

:class:`ConcurrencyLimitMiddleware` keeps a count of requests in flight. While the count
is below the limit, a request goes through; once the limit is reached, further requests
are answered at once with ``503 service_busy`` in the shared error format, with a
``Retry-After`` header, without queueing and without contacting the provider. So at most
``max_in_flight`` requests use threads and Dropbox at any moment, and a rejected request
costs only a small JSON response.

The count is per process: with several uvicorn workers, each enforces its own limit.
The interactive docs (``/docs``, ``/openapi.json``) are not counted, so Swagger UI stays
usable under load.
"""

import os

from fastapi import status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from cloud_storage_service.errors import ErrorDetail, ErrorResponse, log_failure

MAX_IN_FLIGHT_ENV_VAR = "MAX_IN_FLIGHT_REQUESTS"
# Assumption, not a measurement: well below the 40 worker threads, and small enough that a
# burst does not turn into a burst of Dropbox calls. Tune it with the load demo in
# ``docs/overload-protection.md``.
DEFAULT_MAX_IN_FLIGHT = 10
RETRY_AFTER_SECONDS = 1
UNCOUNTED_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
BUSY_MESSAGE = "The service is handling too many requests; retry after a short wait"


def max_in_flight_from_env() -> int:
    """Return the limit from ``MAX_IN_FLIGHT_REQUESTS``, or the default when it is unset.

    Raises:
        ValueError: If the variable is set but is not a positive whole number, so a typo
            stops the service at startup instead of silently disabling the limit.

    """
    raw = os.environ.get(MAX_IN_FLIGHT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MAX_IN_FLIGHT
    if not raw.isdigit() or int(raw) < 1:
        msg = f"{MAX_IN_FLIGHT_ENV_VAR} must be a positive whole number, got {raw!r}"
        raise ValueError(msg)
    return int(raw)


class ConcurrencyLimitMiddleware:
    """ASGI middleware that rejects requests with ``503`` while ``max_in_flight`` are running."""

    def __init__(self, app: ASGIApp, max_in_flight: int) -> None:
        """Wrap ``app``, allowing at most ``max_in_flight`` requests through at a time."""
        if max_in_flight < 1:
            msg = f"max_in_flight must be at least 1, got {max_in_flight}"
            raise ValueError(msg)
        self.app = app
        self.max_in_flight = max_in_flight
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, or answer ``503 service_busy`` if the limit is reached."""
        if scope["type"] != "http" or scope["path"] in UNCOUNTED_PATHS:
            await self.app(scope, receive, send)
            return

        # The check and the increment run without an ``await`` in between, so on the single
        # event loop no other request can slip in between them.
        if self.in_flight >= self.max_in_flight:
            await self._reject(scope, receive, send)
            return

        self.in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        operation = f"{scope['method']} {scope['path']}"
        reason = f"{self.in_flight} requests in flight (limit {self.max_in_flight})"
        log_failure(
            operation,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "service_busy",
            reason,
            provider_failure=False,
        )
        body = ErrorResponse(error=ErrorDetail(code="service_busy", message=BUSY_MESSAGE))
        response = JSONResponse(
            body.model_dump(),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
        await response(scope, receive, send)
