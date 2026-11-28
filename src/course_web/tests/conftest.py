"""A fake API service for the interface's tests: the routes the interface calls, in memory."""

import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from course_web.app import CSRF_COOKIE, app
from course_web.backend import Agent, Api
from course_web.config import Settings

GOOGLE_URL = "https://accounts.google.com/o/oauth2/v2/auth?state=s1"
TITLES = {"dropbox": "Dropbox", "google_calendar": "Google Calendar", "trello": "Trello"}
PROVIDER_URLS = {
    "dropbox": "https://www.dropbox.com/oauth2/authorize",
    "google_calendar": "https://accounts.google.com/o/oauth2/v2/auth",
    "trello": "https://trello.com/1/authorize",
}
# What each provider sends back when the person allows access (Trello: the token itself).
GOOD_CODES = {"dropbox": "dbx-code", "google_calendar": "g-code", "trello": "trello-token"}
SESSION_TOKEN = "session-token-1"  # noqa: S105 (a test value)
AGENT_TOKEN = "agent-token-1"  # noqa: S105 (a test value)
EVENT = {
    "op": "create_event",
    "course": "CS-GY 6083",
    "title": "[CS-GY 6083] Homework 1, Dec 3",
    "start": "2026-12-03T23:00:00-05:00",
    "end": "2026-12-03T23:59:00-05:00",
}
CARD = {"op": "create_issue", "course": "CS-GY 6083", "title": "[CS-GY 6083] Homework 1, Dec 3"}
PLAN = {
    "op": "write_document",
    "course": "CS-GY 6083",
    "name": "course plan.md",
    "content": "# CS-GY 6083\n\n- Homework 1: Dec 3 (10%)\n",
}


def changes_hash(changes: list[dict[str, Any]]) -> str:
    """Return the API's hash of a proposal's content (proposals.py)."""
    text = json.dumps(changes, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "Refused"}})


@dataclass
class FakeApi:
    """Answers like the API service; records each call so tests can check what was sent."""

    calls: list[tuple[str, str, str | None, Any]] = field(default_factory=list)
    courses: list[dict[str, Any]] = field(default_factory=list)
    valid_tokens: set[str] = field(default_factory=lambda: {SESSION_TOKEN})
    down: bool = False
    fail: dict[str, tuple[int, str]] = field(default_factory=dict)
    # Each provider's state at the API; Trello needs a list chosen after connecting.
    connected: dict[str, str] = field(default_factory=dict)
    conversations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    proposals: dict[str, dict[str, Any]] = field(default_factory=dict)
    # What running a proposal does to each change; a test can make one fail or be uncertain.
    execute_as: dict[int, str] = field(default_factory=dict)
    trello_lists: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"board": "Fall 2026", "id": "list-1", "name": "To do"},
            {"board": "Fall 2026", "id": "list-2", "name": "Doing"},
            {"board": "Personal", "id": "list-3", "name": "Errands"},
        ],
    )

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: C901, PLR0911 (one per route)
        """Answer one request."""
        token = request.headers.get("authorization", "").removeprefix("Bearer ") or None
        body = json.loads(request.content) if request.content else None
        route = f"{request.method} {request.url.path}"
        self.calls.append((request.method, request.url.path, token, body))
        if self.down:
            msg = "connection refused"
            raise httpx.ConnectError(msg, request=request)
        if route in self.fail:
            status, code = self.fail[route]
            return _error(status, code)
        if route == "POST /auth/google/start":
            return httpx.Response(200, json={"authorization_url": GOOGLE_URL, "binding": "b1"})
        if route == "POST /auth/google/finish":
            ok = body == {"state": "s1", "code": "c1", "binding": "b1"}
            if not ok:
                return _error(400, "sign_in_failed")
            return httpx.Response(200, json={"session_token": SESSION_TOKEN, "user": {}})
        if token not in self.valid_tokens:
            return _error(401, "unauthorized")
        if route == "POST /auth/sign-out":
            self.valid_tokens.discard(token)
            return httpx.Response(204)
        if route == "GET /courses":
            return httpx.Response(200, json=self.courses)
        if route == "POST /auth/agent-token":
            return httpx.Response(200, json={"token": AGENT_TOKEN, "expires_at": "2026-12-01"})
        if request.url.path.startswith(("/conversations", "/proposals")):
            return self.handle_work(request.method, request.url.path.strip("/").split("/"), body)
        return self.handle_connections(request.method, request.url.path, body)

    def add_proposal(self, changes: list[dict[str, Any]], status: str = "pending") -> str:
        """Store a proposal as the API would; return its ID."""
        proposal_id = str(uuid.uuid4())
        self.proposals[proposal_id] = {
            "id": proposal_id,
            "status": status,
            "changes": changes,
            "changes_hash": changes_hash(changes),
            "created_at": "2026-11-27T12:00:00Z",
            "decided_at": None,
            "outcomes": [],
        }
        return proposal_id

    def handle_work(  # noqa: C901, PLR0911, PLR0912 (one branch per route)
        self,
        method: str,
        parts: list[str],
        body: Any,  # noqa: ANN401 (JSON)
    ) -> httpx.Response:
        """Answer the conversation and proposal routes, as their route modules do."""
        if parts == ["conversations"] and method == "POST":
            conversation_id = str(uuid.uuid4())
            self.conversations[conversation_id] = []
            return httpx.Response(201, json={"id": conversation_id, "title": "", "created_at": ""})
        if parts[0] == "conversations":
            if parts[1] not in self.conversations:
                return _error(404, "not_found")
            return httpx.Response(200, json=self.conversations[parts[1]])
        if parts == ["proposals"] and method == "POST":
            return httpx.Response(201, json=self.proposals[self.add_proposal(body["changes"])])
        proposal = self.proposals.get(parts[1])
        if proposal is None:
            return _error(404, "not_found")
        action = parts[2] if len(parts) > 2 else ""  # noqa: PLR2004
        if action == "approve":
            if proposal["status"] != "pending" or body["changes_hash"] != proposal["changes_hash"]:
                return _error(409, "conflict")
            proposal["status"] = "approved"
        elif action == "reject":
            if proposal["status"] != "pending":
                return _error(409, "conflict")
            proposal["status"] = "rejected"
        elif action == "execute":
            if proposal["status"] not in {"approved", "failed"}:
                return _error(409, "conflict")
            outcomes = [
                {"change_index": i, "status": self.execute_as.get(i, "completed"), "detail": ""}
                for i in range(len(proposal["changes"]))
            ]
            proposal["outcomes"] = outcomes
            done = all(o["status"] == "completed" for o in outcomes)
            proposal["status"] = "done" if done else "failed"
        elif action == "changes":
            outcome = proposal["outcomes"][int(parts[3])]
            if outcome["status"] != "uncertain":
                return _error(409, "conflict")
            outcome["status"] = body["status"]
        return httpx.Response(200, json=proposal)

    def _view(self, provider: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "title": TITLES[provider],
            "available": True,
            "status": self.connected.get(provider, "not_connected"),
            "account": "student@example.com" if provider in self.connected else "",
        }

    def handle_connections(  # noqa: PLR0911 (one per route)
        self,
        method: str,
        path: str,
        body: Any,  # noqa: ANN401 (JSON)
    ) -> httpx.Response:
        """Answer the connection routes, as the API service's connection_routes.py does."""
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/connections":
            return httpx.Response(200, json=[self._view(p) for p in TITLES])
        if method == "GET" and path == "/connections/trello/lists":
            return httpx.Response(200, json=self.trello_lists)
        if method == "PUT" and path == "/connections/trello/list":
            if body.get("list_id") not in {t["id"] for t in self.trello_lists}:
                return _error(400, "invalid_request")
            self.connected["trello"] = "connected"
            return httpx.Response(200, json=self._view("trello"))
        if len(parts) < 2 or parts[0] != "connections" or parts[1] not in TITLES:  # noqa: PLR2004
            return _error(404, "not_found")
        provider = parts[1]
        if method == "POST" and parts[2:] == ["start"]:
            url = f"{PROVIDER_URLS[provider]}?state=st-{provider}"
            return httpx.Response(200, json={"authorization_url": url})
        if method == "POST" and parts[2:] == ["finish"]:
            if body != {"state": f"st-{provider}", "code": GOOD_CODES[provider]}:
                return _error(400, "connect_failed")
            self.connected[provider] = "setup_needed" if provider == "trello" else "connected"
            return httpx.Response(200, json=self._view(provider))
        if method == "DELETE" and len(parts) == 2:  # noqa: PLR2004
            self.connected.pop(provider, None)
            return httpx.Response(204)
        return _error(404, "not_found")


@pytest.fixture
def fake_api() -> Iterator[FakeApi]:
    """Point the app at a fake API service for one test."""
    fake = FakeApi()
    http = httpx.Client(transport=httpx.MockTransport(fake.handle), base_url="http://api.test")
    app.state.api, app.state.settings = Api(http), Settings(api_base_url="http://api.test")
    yield fake
    del app.state.api, app.state.settings
    http.close()


@dataclass
class FakeAgent:
    """Answers like the agent service: one turn stores the student's message and a reply."""

    api: FakeApi
    calls: list[tuple[str, str | None, Any]] = field(default_factory=list)
    fail: tuple[int, str] | None = None
    # The next turn prepares a proposal with these changes, instead of only replying.
    propose: list[dict[str, Any]] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one turn."""
        token = request.headers.get("authorization", "").removeprefix("Bearer ") or None
        body = json.loads(request.content)
        self.calls.append((request.url.path, token, body))
        if self.fail:
            return _error(*self.fail)
        if token != AGENT_TOKEN:
            return _error(401, "unauthorized")
        conversation = self.api.conversations.get(request.url.path.split("/")[2])
        if conversation is None:
            return _error(404, "not_found")
        proposal_id = self.api.add_proposal(self.propose) if self.propose else None
        reply = "I've prepared changes for you to review." if proposal_id else "Which course?"
        run = {"stop_reason": "proposal_ready" if proposal_id else "replied"}
        if proposal_id:
            run["proposal_id"] = proposal_id
        conversation += [
            {"seq": len(conversation), "role": "user", "content": {"text": body["text"]}},
            {"seq": 0, "role": "assistant", "content": {"text": "", "tool_calls": [{"id": "1"}]}},
            {"seq": 0, "role": "tool", "content": {"text": "{}", "name": "list_courses"}},
            {"seq": 0, "role": "assistant", "content": {"text": reply, "run": run}},
        ]
        return httpx.Response(
            200,
            json={"stop_reason": run["stop_reason"], "reply": reply, "proposal_id": proposal_id},
        )


@pytest.fixture
def fake_agent(fake_api: FakeApi) -> Iterator[FakeAgent]:
    """Point the app at a fake agent service, next to the fake API service."""
    fake = FakeAgent(fake_api)
    http = httpx.Client(transport=httpx.MockTransport(fake.handle), base_url="http://agent.test")
    app.state.agent = Agent(http)
    yield fake
    del app.state.agent
    http.close()


@pytest.fixture
def browser() -> TestClient:
    """Return a browser on https that keeps cookies and doesn't follow redirects."""
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def csrf(browser: TestClient) -> str:
    """Load a page so the browser gets its CSRF cookie, and return the value."""
    browser.get("/")
    value = browser.cookies.get(CSRF_COOKIE)
    assert isinstance(value, str)
    assert value
    return value
