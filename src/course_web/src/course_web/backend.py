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


@dataclass(frozen=True)
class Connection:
    """One provider as the connections page shows it: never a token."""

    provider: str
    title: str
    available: bool
    status: str
    account: str


@dataclass(frozen=True)
class TrelloList:
    """A Trello list the person can send their cards to."""

    board: str
    id: str
    name: str


def _connection(c: dict[str, Any]) -> Connection:
    return Connection(
        provider=str(c["provider"]),
        title=str(c["title"]),
        available=bool(c["available"]),
        status=str(c["status"]),
        account=str(c.get("account") or ""),
    )


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

    def connections(self, token: str) -> list[Connection]:
        """Return each provider and the person's connection to it."""
        return [_connection(c) for c in self._call("GET", "/connections", token=token)]

    def start_connection(self, token: str, provider: str) -> str:
        """Begin connecting ``provider``; return where to send the browser."""
        body = self._call("POST", f"/connections/{provider}/start", token=token)
        return str(body["authorization_url"])

    def finish_connection(self, token: str, provider: str, state: str, code: str) -> Connection:
        """Finish connecting ``provider`` with what it sent back (Trello: the token)."""
        c = self._call(
            "POST",
            f"/connections/{provider}/finish",
            token=token,
            body={"state": state, "code": code},
        )
        return _connection(c)

    def disconnect(self, token: str, provider: str) -> None:
        """Delete the stored connection and revoke it at the provider."""
        self._call("DELETE", f"/connections/{provider}", token=token)

    def trello_lists(self, token: str) -> list[TrelloList]:
        """Return the open lists on the person's open Trello boards."""
        return [
            TrelloList(board=str(t["board"]), id=str(t["id"]), name=str(t["name"]))
            for t in self._call("GET", "/connections/trello/lists", token=token)
        ]

    def choose_trello_list(self, token: str, list_id: str) -> None:
        """Choose the list the person's cards go to."""
        self._call("PUT", "/connections/trello/list", token=token, body={"list_id": list_id})
