"""A fake API service for the interface's tests: the routes the interface calls, in memory."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from course_web.app import CSRF_COOKIE, app
from course_web.backend import Api
from course_web.config import Settings

GOOGLE_URL = "https://accounts.google.com/o/oauth2/v2/auth?state=s1"
SESSION_TOKEN = "session-token-1"  # noqa: S105 (a test value)


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

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 (one per route)
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
