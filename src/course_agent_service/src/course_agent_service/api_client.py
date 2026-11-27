"""The agent service's only way to reach user data: the API service, over HTTP.

The agent never holds a provider credential or a database connection. Every call carries the
caller's identity headers unchanged (``X-Dev-User`` until sign-in lands in #96 and #97), so the
API service decides who the user is and what they may see, on every request (DESIGN.md, P1).
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

# The headers that identify the caller. Only these are forwarded; nothing else from the
# incoming request reaches the API service. The interface sends an agent token
# (``Authorization: Bearer``, read and propose only), never the person's session.
IDENTITY_HEADERS = ("authorization", "x-dev-user")


class ApiError(Exception):
    """The API service refused or failed a call. ``code`` is the shared error code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Record the status, code and a message safe to show."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def identity_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return the identity headers from an incoming request, and nothing else."""
    return {name: headers[name] for name in IDENTITY_HEADERS if name in headers}


def _segment(value: str) -> str:
    # Encode every reserved character, "/" included, so a value is always one path segment.
    return quote(value, safe="")


class ApiClient:
    """The API service's routes the agent uses, acting as one caller."""

    def __init__(self, http: httpx.Client, identity: Mapping[str, str]) -> None:
        """Bind an HTTP client (its ``base_url`` is the API service) and the caller's headers."""
        self._http = http
        self._identity = dict(identity)

    def _call(self, method: str, path: str, body: Any = None) -> Any:  # noqa: ANN401 (JSON)
        try:
            response = self._http.request(method, path, json=body, headers=self._identity)
        except httpx.HTTPError as e:
            msg = f"The API service is unreachable ({type(e).__name__})"
            raise ApiError(503, "api_unavailable", msg) from None
        if response.is_success:
            return response.json()
        try:
            error = response.json()["error"]
            code, message = str(error["code"]), str(error["message"])
        except (ValueError, KeyError, TypeError):
            code, message = "api_error", f"The API service answered {response.status_code}"
        raise ApiError(response.status_code, code, message)

    def list_courses(self) -> list[dict[str, Any]]:
        """``GET /courses``."""
        result: list[dict[str, Any]] = self._call("GET", "/courses")
        return result

    def list_course_documents(self, course: str) -> list[dict[str, Any]]:
        """``GET /courses/{course}/documents``."""
        result: list[dict[str, Any]] = self._call("GET", f"/courses/{_segment(course)}/documents")
        return result

    def read_document(self, course: str, name: str) -> dict[str, Any]:
        """``GET /courses/{course}/documents/{name}``."""
        path = f"/courses/{_segment(course)}/documents/{_segment(name)}"
        result: dict[str, Any] = self._call("GET", path)
        return result

    def get_course_plan(self, course: str) -> dict[str, Any]:
        """``GET /courses/{course}/plan``."""
        result: dict[str, Any] = self._call("GET", f"/courses/{_segment(course)}/plan")
        return result

    def create_proposal(
        self,
        changes: list[dict[str, Any]],
        conversation_id: str | None,
    ) -> dict[str, Any]:
        """``POST /proposals``: store a pending change set. Changes nothing in a provider."""
        result: dict[str, Any] = self._call(
            "POST",
            "/proposals",
            {"changes": changes, "conversation_id": conversation_id},
        )
        return result

    def create_conversation(self, title: str) -> dict[str, Any]:
        """``POST /conversations``."""
        result: dict[str, Any] = self._call("POST", "/conversations", {"title": title})
        return result

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        """``GET /conversations/{id}/messages``."""
        path = f"/conversations/{_segment(conversation_id)}/messages"
        result: list[dict[str, Any]] = self._call("GET", path)
        return result

    def append_messages(self, conversation_id: str, messages: list[dict[str, Any]]) -> None:
        """``POST /conversations/{id}/messages``."""
        path = f"/conversations/{_segment(conversation_id)}/messages"
        self._call("POST", path, {"messages": messages})
