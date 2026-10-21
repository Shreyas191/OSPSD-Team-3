"""The team's shared error format for HTTP responses: ``{"error": {"code", "message"}}``.

Routes registered on a router with ``route_class=SharedErrorRoute`` report every failure
in this format. Request validation errors and the built-in exceptions the ``Client``
contract documents are mapped onto a status code and a stable machine-readable ``code``,
and each failure is logged as a structured warning. Any other exception is a bug and is
left alone, so it still surfaces as ``500``.

| Exception (or cause)                  | Status | ``code``                 |
|---------------------------------------|--------|--------------------------|
| request validation / ``ValueError``   | 422    | ``invalid_input``        |
| ``IsADirectoryError`` (folder given   | 422    | ``invalid_input``        |
| where a file is needed, e.g. download)|        |                          |
| ``FileNotFoundError``                 | 404    | ``not_found``            |
| ``FileExistsError``                   | 409    | ``conflict``             |
| ``PermissionError``                   | 403    | ``permission_denied``    |
| ``ConnectionError``                   | 503    | ``provider_unavailable`` |
| too many requests in flight (overload)| 503    | ``service_busy``         |
| any other ``OSError``                 | 502    | ``provider_failure``     |

For ``502`` and ``503`` the response carries a fixed message, so provider details never
reach the caller; the exception's own message is kept in the warning log.
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    """What went wrong: a stable ``code`` for programs and a ``message`` for people."""

    code: str = Field(examples=["not_found"])
    message: str = Field(examples=["No file or folder at '/Docs/report.pdf'"])


class ErrorResponse(BaseModel):
    """The body of every error response from a route using :class:`SharedErrorRoute`."""

    error: ErrorDetail


class SharedErrorRoute(APIRoute):
    """Route class that reports failures in the shared error format and logs them."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap FastAPI's handler so validation and contract errors become error responses."""
        handle_request = super().get_route_handler()
        operation = self.name

        async def handle(request: Request) -> Response:
            try:
                return await handle_request(request)
            except RequestValidationError as e:
                status_code, code = status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input"
                message = reason = _describe(e)
            except (ValueError, OSError) as e:
                status_code, code, message = _classify(e)
                reason = str(e)
            log_failure(operation, status_code, code, reason)
            body = ErrorResponse(error=ErrorDetail(code=code, message=message))
            return JSONResponse(body.model_dump(), status_code=status_code)

        return handle


def _classify(error: ValueError | OSError) -> tuple[int, str, str]:
    """Return the status code, error code and caller-facing message for a contract exception."""
    if isinstance(error, ValueError | IsADirectoryError):
        return status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_input", str(error)
    if isinstance(error, FileNotFoundError):
        return status.HTTP_404_NOT_FOUND, "not_found", str(error)
    if isinstance(error, FileExistsError):
        return status.HTTP_409_CONFLICT, "conflict", str(error)
    if isinstance(error, PermissionError):
        return status.HTTP_403_FORBIDDEN, "permission_denied", str(error)
    if isinstance(error, ConnectionError):
        message = "The storage provider is unreachable or busy; try again later"
        return status.HTTP_503_SERVICE_UNAVAILABLE, "provider_unavailable", message
    message = "The storage provider failed to complete the request"
    return status.HTTP_502_BAD_GATEWAY, "provider_failure", message


def _describe(error: RequestValidationError) -> str:
    """Summarise validation errors as ``location: problem`` pairs, without the rejected input."""
    problems = (
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
    )
    return "; ".join(problems)


def log_failure(
    operation: str,
    status_code: int,
    code: str,
    reason: str,
    *,
    provider_failure: bool | None = None,
) -> None:
    """Log a failed request as ``key=value`` pairs, also attached to the record as ``extra``.

    ``provider_failure`` defaults to "the status is a 5xx". Callers that answer with a 5xx
    the provider did not cause (e.g. the service shedding load) pass ``False``.
    """
    if provider_failure is None:
        provider_failure = status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR
    fields = {
        "operation": operation,
        "status": status_code,
        "code": code,
        "provider_failure": provider_failure,
        "reason": reason,
    }
    pairs = " ".join(f"{key}={value!r}" for key, value in fields.items())
    logger.warning("Request failed: %s", pairs, extra=fields)
