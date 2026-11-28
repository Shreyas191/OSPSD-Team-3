"""The API and agent services' routes the interface calls, as one signed-in person.

The interface decides nothing: every check happens in the API service, which gets the
person's session token in ``Authorization: Bearer`` on every call (DESIGN.md, P4 and P5).
A conversation turn goes to the agent service instead, with a short-lived agent token the
API makes from the session for that one turn: it can read and propose, never approve or
execute (identity.py), so the agent service never holds the person's session.

Errors from either service come back as ``BackendError`` with its status and error code; a
network failure is a ``503`` with the code ``unreachable``.
"""

from dataclasses import dataclass
from typing import Any

import httpx

TIMEOUT_SECONDS = 20.0
# A turn runs up to 60 s at the agent (loop.py), after waiting for a free slot; executing a
# proposal is one write and one read-back per change. Cloud Run allows a request 300 s.
AGENT_TIMEOUT_SECONDS = 150.0
EXECUTE_TIMEOUT_SECONDS = 240.0


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


@dataclass(frozen=True)
class ChatEntry:
    """One message the person sees in a conversation: theirs, or the assistant's reply.

    ``proposal_id`` is set on the reply of a turn that prepared changes to review.
    """

    role: str  # "student" or "assistant"
    text: str
    proposal_id: str | None = None


@dataclass(frozen=True)
class Outcome:
    """What happened to one change when the proposal ran."""

    change_index: int
    status: str  # started, completed, failed or uncertain
    detail: str


@dataclass(frozen=True)
class Proposal:
    """A proposal exactly as stored: its changes, the hash approval names, and outcomes."""

    id: str
    status: str  # pending, approved, rejected, executing, failed or done
    changes: list[dict[str, Any]]
    changes_hash: str
    outcomes: dict[int, Outcome]


@dataclass(frozen=True)
class TurnResult:
    """How a turn ended (the loop's stop reason) and the proposal it prepared, if any."""

    stop_reason: str
    reply: str
    proposal_id: str | None


def chat_entries(stored: list[dict[str, Any]]) -> list[ChatEntry]:
    """Return the messages a person sees, from a conversation's stored messages.

    The student's messages and the assistant's text replies; the model's tool calls and the
    tool results are the agent's working, so they aren't shown.
    """
    entries = []
    for message in stored:
        content = message.get("content") or {}
        text = str(content.get("text") or "")
        if message.get("role") == "user":
            entries.append(ChatEntry("student", text))
        elif message.get("role") == "assistant" and text and not content.get("tool_calls"):
            proposal = (content.get("run") or {}).get("proposal_id")
            entries.append(ChatEntry("assistant", text, str(proposal) if proposal else None))
    return entries


def _proposal(p: dict[str, Any]) -> Proposal:
    return Proposal(
        id=str(p["id"]),
        status=str(p["status"]),
        changes=list(p["changes"]),
        changes_hash=str(p["changes_hash"]),
        outcomes={
            int(o["change_index"]): Outcome(
                change_index=int(o["change_index"]),
                status=str(o["status"]),
                detail=str(o.get("detail") or ""),
            )
            for o in p.get("outcomes", [])
        },
    )


def _connection(c: dict[str, Any]) -> Connection:
    return Connection(
        provider=str(c["provider"]),
        title=str(c["title"]),
        available=bool(c["available"]),
        status=str(c["status"]),
        account=str(c.get("account") or ""),
    )


class _Service:
    """One of our services over HTTP, answering with the shared error body on failure."""

    def __init__(self, http: httpx.Client) -> None:
        """Use ``http``, whose ``base_url`` is the service."""
        self._http = http

    def _call(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:  # noqa: ANN401 (the JSON the route returns)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            response = self._http.request(method, path, headers=headers, json=body, **extra)
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


class Api(_Service):
    """A client for the API service: sign-in, courses, connections, conversations, proposals."""

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

    def agent_token(self, token: str) -> str:
        """Make a read-and-propose token for one agent turn, from this session."""
        return str(self._call("POST", "/auth/agent-token", token=token)["token"])

    def create_conversation(self, token: str) -> str:
        """Start a conversation; return its ID."""
        return str(self._call("POST", "/conversations", token=token, body={"title": ""})["id"])

    def conversation(self, token: str, conversation_id: str) -> list[ChatEntry]:
        """Return what the person sees of one of their conversations."""
        return chat_entries(
            self._call("GET", f"/conversations/{conversation_id}/messages", token=token),
        )

    def proposal(self, token: str, proposal_id: str) -> Proposal:
        """Return one of the person's proposals, with its outcomes."""
        return _proposal(self._call("GET", f"/proposals/{proposal_id}", token=token))

    def create_proposal(
        self,
        token: str,
        changes: list[dict[str, Any]],
        conversation_id: str | None = None,
    ) -> Proposal:
        """Store a new pending proposal with exactly these changes, in that conversation."""
        body: dict[str, Any] = {"changes": changes}
        if conversation_id:
            body["conversation_id"] = conversation_id
        return _proposal(self._call("POST", "/proposals", token=token, body=body))

    def approve(self, token: str, proposal_id: str, changes_hash: str) -> Proposal:
        """Approve exactly the content with this hash."""
        body = {"changes_hash": changes_hash}
        return _proposal(
            self._call("POST", f"/proposals/{proposal_id}/approve", token=token, body=body),
        )

    def reject(self, token: str, proposal_id: str) -> Proposal:
        """Reject a pending proposal."""
        return _proposal(self._call("POST", f"/proposals/{proposal_id}/reject", token=token))

    def execute(self, token: str, proposal_id: str) -> Proposal:
        """Run an approved proposal, or retry a failed one."""
        return _proposal(
            self._call(
                "POST",
                f"/proposals/{proposal_id}/execute",
                token=token,
                timeout=EXECUTE_TIMEOUT_SECONDS,
            ),
        )

    def resolve(
        self,
        token: str,
        proposal_id: str,
        change_index: int,
        status: str,
        record_id: str | None,
    ) -> Proposal:
        """Settle an uncertain change: it happened (with the created record's ID) or not."""
        body: dict[str, Any] = {"status": status}
        if record_id:
            body["record_id"] = record_id
        path = f"/proposals/{proposal_id}/changes/{change_index}/resolve"
        return _proposal(self._call("POST", path, token=token, body=body))


class Agent(_Service):
    """A client for the agent service: one conversation turn at a time."""

    def take_turn(self, agent_token: str, conversation_id: str, text: str) -> TurnResult:
        """Run one turn of the person's conversation with what they wrote."""
        result = self._call(
            "POST",
            f"/conversations/{conversation_id}/turns",
            token=agent_token,
            body={"text": text},
            timeout=AGENT_TIMEOUT_SECONDS,
        )
        return TurnResult(
            stop_reason=str(result["stop_reason"]),
            reply=str(result["reply"]),
            proposal_id=str(result["proposal_id"]) if result.get("proposal_id") else None,
        )
