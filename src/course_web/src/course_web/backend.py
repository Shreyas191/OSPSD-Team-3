"""The API service's routes the interface calls, as one signed-in person or anonymously.

The interface decides nothing: every check happens in the API service, which gets the
person's session token in ``Authorization: Bearer`` on every call (DESIGN.md, P4 and P5).
Errors come back as ``BackendError`` with the API's status and error code; a network failure
is a ``503`` with the code ``unreachable``.
"""

from dataclasses import dataclass
from typing import Any

import httpx

TIMEOUT_SECONDS = 20.0


class BackendError(Exception):
    """The API service refused the request, failed, or couldn't be reached."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        """Record the HTTP status, the shared error code and the API's message."""
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SignInStart:
    """Where to send the browser for Google sign-in, and the value its cookie keeps."""

    authorization_url: str
    binding: str


@dataclass(frozen=True)
class Course:
    """One of the person's courses, with how many records our proposals created for it."""

    course: str
    events: int
    issues: int
    documents: int


class Api:
    """A client for the API service's sign-in and course routes."""

    def __init__(self, http: httpx.Client) -> None:
        """Use ``http``, whose ``base_url`` is the API service."""
        self._http = http

    def _call(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401 (the JSON the route returns)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self._http.request(method, path, headers=headers, json=body)
        except httpx.HTTPError:
            raise BackendError(503, "unreachable") from None
        if response.is_error:
            code, message = "error", ""
            try:
                error = response.json()["error"]
                code, message = str(error["code"]), str(error["message"])
            except (ValueError, KeyError, TypeError):
                pass
            raise BackendError(response.status_code, code, message)
        return None if response.status_code == httpx.codes.NO_CONTENT else response.json()

    def start_sign_in(self) -> SignInStart:
        """Begin a Google sign-in."""
        data = self._call("POST", "/auth/google/start")
        return SignInStart(str(data["authorization_url"]), str(data["binding"]))

    def finish_sign_in(self, state: str, code: str, binding: str) -> str:
        """Finish a Google sign-in; return the new session token."""
        data = self._call(
            "POST",
            "/auth/google/finish",
            body={"state": state, "code": code, "binding": binding},
        )
        return str(data["session_token"])

    def sign_out(self, token: str) -> None:
        """End this session at the API service."""
        self._call("POST", "/auth/sign-out", token=token)

    def courses(self, token: str) -> list[Course]:
        """Return the person's courses."""
        return [
            Course(
                course=str(c["course"]),
                events=int(c["events"]),
                issues=int(c["issues"]),
                documents=int(c["documents"]),
            )
            for c in self._call("GET", "/courses", token=token)
        ]
