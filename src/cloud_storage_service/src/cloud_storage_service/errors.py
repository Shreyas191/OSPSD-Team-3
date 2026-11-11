"""The vertical's shared error format for HTTP responses: ``{"error": {"code", "message"}}``.

Every error the service returns uses this body. Two places produce it:

- :class:`SharedErrorRoute` handles requests that reach a route. Request validation errors
  and the built-in exceptions the ``Client`` contract documents are mapped onto a status
  and a stable ``code``, and each failure is logged as a structured warning.
- :func:`add_error_handlers` handles everything else: an unknown route, a method the route
  does not allow, and any unexpected exception (a bug).

| Exception (or cause)                    | Status | ``code``                 |
|-----------------------------------------|--------|--------------------------|
| request validation (bad path, missing   | 400    | ``invalid_request``      |
| field, body that is not JSON)           |        |                          |
| ``FileTooLargeError``                   | 413    | ``file_too_large``       |
| ``ValueError``, ``IsADirectoryError``   | 422    | ``invalid_operation``    |
| ``FileNotFoundError``                   | 404    | ``not_found``            |
| ``FileExistsError``                     | 409    | ``conflict``             |
| ``PermissionError``                     | 403    | ``permission_denied``    |
| ``ConnectionError``                     | 503    | ``provider_unavailable`` |
| too many requests in flight (overload)  | 503    | ``service_busy``         |
| any other ``OSError``                   | 502    | ``provider_error``       |
| no such route                           | 404    | ``route_not_found``      |
| method not allowed on the route         | 405    | ``method_not_allowed``   |
| unexpected exception                    | 500    | ``internal_error``       |

For ``5xx`` responses the message is fixed, so provider details never reach the caller;
the exception's own message is kept in the warning log.
"""

from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from cloud_storage_client_api import FileTooLargeError
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from cloud_storage_service.telemetry import record_failure


class ErrorDetail(BaseModel):
    """What went wrong: a stable ``code`` for programs and a ``message`` for people."""

    code: str = Field(examples=["not_found"])
    message: str = Field(examples=["No file or folder at '/Docs/report.pdf'"])


class ErrorResponse(BaseModel):
    """The body of every error response."""

    error: ErrorDetail


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Return a response with the shared error body."""
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(body.model_dump(), status_code=status_code, headers=headers)


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
                status_code, code = status.HTTP_400_BAD_REQUEST, "invalid_request"
                message = reason = _describe(e)
            except (ValueError, OSError) as e:
                status_code, code, message = _classify(e)
                reason = str(e)
            record_failure(operation, status_code, code, reason)
            return error_response(status_code, code, message)

        return handle


def add_error_handlers(app: FastAPI) -> None:
    """Make the errors no route handles use the shared body too."""

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        # Starlette raises these for 404 (no route) and 405 (wrong method); any other
        # status keeps its own code and is reported as an invalid request.
        known = {
            status.HTTP_404_NOT_FOUND: ("route_not_found", f"No route {request.url.path}"),
            status.HTTP_405_METHOD_NOT_ALLOWED: (
                "method_not_allowed",
                f"{request.method} is not allowed on {request.url.path}",
            ),
        }
        code, message = known.get(exc.status_code, ("invalid_request", str(exc.detail)))
        record_failure(f"{request.method} {request.url.path}", exc.status_code, code, message)
        return error_response(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(Exception)
    def unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        # The request's log line (telemetry.RequestTelemetryMiddleware) already records the
        # exception: this handler runs after that middleware has finished.
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return error_response(status_code, "internal_error", "The service failed unexpectedly")


# Client errors keep the client's message. Checked in order: FileTooLargeError is a
# ValueError, so it comes first.
_CLIENT_ERRORS: tuple[tuple[type[Exception] | tuple[type[Exception], ...], int, str], ...] = (
    (FileTooLargeError, status.HTTP_413_CONTENT_TOO_LARGE, "file_too_large"),
    ((ValueError, IsADirectoryError), status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_operation"),
    (FileNotFoundError, status.HTTP_404_NOT_FOUND, "not_found"),
    (FileExistsError, status.HTTP_409_CONFLICT, "conflict"),
    (PermissionError, status.HTTP_403_FORBIDDEN, "permission_denied"),
)


def _classify(error: ValueError | OSError) -> tuple[int, str, str]:
    """Return the status code, error code and caller-facing message for a contract exception."""
    for kinds, status_code, code in _CLIENT_ERRORS:
        if isinstance(error, kinds):
            return status_code, code, str(error)
    if isinstance(error, ConnectionError):
        message = "The storage provider is unreachable or busy; try again later"
        return status.HTTP_503_SERVICE_UNAVAILABLE, "provider_unavailable", message
    message = "The storage provider failed to complete the request"
    return status.HTTP_502_BAD_GATEWAY, "provider_error", message


def _describe(error: RequestValidationError) -> str:
    """Summarise validation errors as ``location: problem`` pairs, without the rejected input."""
    problems = (
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
    )
    return "; ".join(problems)
