"""Google sign-in, sessions and agent tokens (#96, #97), over HTTP, with a fake Google.

Google's token endpoint is an ``httpx.MockTransport``, and its ID token check is replaced by
one that reads the claims the fake put in the token (the real one checks Google's signature,
audience and expiry with google-auth). Everything else is the real code: the attempt store,
PKCE, binding, nonce, the session table and the scope checks.

Each refusal has a valid control next to it (threats T7, T9 and T14).
"""

import json
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from course_autopilot_store import Database
from course_autopilot_store.models import SignInAttempt, User, UserSession
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.pool import StaticPool

from cloud_storage_service.app import app
from cloud_storage_service.auth_routes import get_google_sign_in
from cloud_storage_service.database import get_database
from cloud_storage_service.dependencies import get_calendar, get_document_storage, get_issue_tracker
from cloud_storage_service.fake_verticals import (
    InMemoryCalendar,
    InMemoryDocumentStorage,
    InMemoryIssueTracker,
)
from cloud_storage_service.identity import DEV_SIGN_IN_ENV_VAR
from cloud_storage_service.sign_in import (
    CLIENT_ID_ENV_VAR,
    CLIENT_SECRET_ENV_VAR,
    REDIRECT_URI_ENV_VAR,
    TOKEN_ENDPOINT,
    GoogleSettings,
    GoogleSignIn,
)

SETTINGS = GoogleSettings("client-123", "secret-xyz", "https://web.example.test/auth/callback")
COURSE = "CS-GY 6083"
ISSUE = {"op": "create_issue", "course": COURSE, "title": "[CS-GY 6083] Midterm, Dec 10"}


class FakeGoogle:
    """Google's token endpoint. The next code exchange answers for ``person``."""

    def __init__(self) -> None:
        """Start with one person, Alice, and a working endpoint."""
        self.person: dict[str, Any] = {"sub": "google-alice", "name": "Alice"}
        self.claims_override: dict[str, Any] = {}
        self.status = 200
        self.unreachable = False
        self.exchanges: list[dict[str, list[str]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer a code exchange with an ID token carrying the claims."""
        if self.unreachable:
            msg = "no route to Google"
            raise httpx.ConnectError(msg, request=request)
        assert str(request.url) == TOKEN_ENDPOINT
        form = parse_qs(request.content.decode())
        self.exchanges.append(form)
        if self.status != httpx.codes.OK:
            return httpx.Response(self.status, json={"error": "invalid_grant"})
        # The code is "code:<nonce>", so the fake can put the nonce Google would echo back.
        nonce = form["code"][0].removeprefix("code:")
        claims = {
            "iss": "https://accounts.google.com",
            "aud": SETTINGS.client_id,
            "nonce": nonce,
            **self.person,
            **self.claims_override,
        }
        return httpx.Response(200, json={"id_token": json.dumps(claims)})


def fake_verify(token: str, audience: str) -> Mapping[str, Any]:
    """Read the fake's claims; refuse another audience, as google-auth does."""
    claims: dict[str, Any] = json.loads(token)
    if claims["aud"] != audience:
        msg = "wrong audience"
        raise ValueError(msg)
    return claims


@pytest.fixture
def database() -> Iterator[Database]:
    """Provide a migrated in-memory database with foreign keys enforced."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    db = Database(engine)
    db.upgrade()
    yield db
    engine.dispose()


@pytest.fixture
def google() -> FakeGoogle:
    """Provide the fake Google."""
    return FakeGoogle()


@pytest.fixture
def client(
    database: Database,
    google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Serve the API app with Google sign-in on, the fake Google, and dev sign-in off."""
    monkeypatch.delenv(DEV_SIGN_IN_ENV_VAR, raising=False)
    sign_in = GoogleSignIn(
        SETTINGS,
        httpx.Client(transport=httpx.MockTransport(google.handle)),
        verify=fake_verify,
    )
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_google_sign_in] = lambda: sign_in
    app.dependency_overrides[get_document_storage] = InMemoryDocumentStorage
    app.dependency_overrides[get_issue_tracker] = InMemoryIssueTracker
    app.dependency_overrides[get_calendar] = InMemoryCalendar
    yield TestClient(app)
    app.dependency_overrides.clear()


def start(client: TestClient) -> tuple[str, str, str]:
    """Begin a sign-in; return the state, the nonce Google would receive, and the binding."""
    body = client.post("/auth/google/start").json()
    query = parse_qs(urlparse(body["authorization_url"]).query)
    return query["state"][0], query["nonce"][0], body["binding"]


def sign_in(client: TestClient) -> dict[str, Any]:
    """Sign in from start to finish; return the session response."""
    state, nonce, binding = start(client)
    response = client.post(
        "/auth/google/finish",
        json={"state": state, "code": f"code:{nonce}", "binding": binding},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(token: str) -> dict[str, str]:
    """Return the header that presents ``token``."""
    return {"Authorization": f"Bearer {token}"}


# Sign-in


def test_start_sends_the_browser_to_google_with_pkce(client: TestClient) -> None:
    """The URL carries our client, the fixed redirect, a state, a nonce and an S256 challenge."""
    url = urlparse(client.post("/auth/google/start").json()["authorization_url"])
    query = parse_qs(url.query)

    assert (
        f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert query["client_id"] == [SETTINGS.client_id]
    assert query["redirect_uri"] == [SETTINGS.redirect_uri]
    assert query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 32
    assert len(query["nonce"][0]) >= 32


def test_a_full_sign_in_gives_a_session_for_product_routes(
    client: TestClient,
    google: FakeGoogle,
    database: Database,
) -> None:
    """A finished sign-in returns a session that product routes accept."""
    body = sign_in(client)

    assert body["user"]["display_name"] == "Alice"
    me = client.get("/auth/me", headers=bearer(body["session_token"])).json()
    assert me == {"user_id": body["user"]["id"], "scope": "session"}
    assert client.get("/auth/me", headers=bearer(body["session_token"])).status_code == 200
    # The code went to Google with the PKCE verifier and our secret, from the server only.
    exchange = google.exchanges[0]
    assert exchange["client_secret"] == [SETTINGS.client_secret]
    assert len(exchange["code_verifier"][0]) >= 43
    # Only the token's hash is stored.
    with database.session() as session:
        stored = session.scalars(select(UserSession.token_hash)).all()
    assert body["session_token"] not in stored
    assert len(stored) == 1


def test_the_same_google_account_is_the_same_user(client: TestClient, google: FakeGoogle) -> None:
    """Signing in again finds the account by Google's subject ID and keeps the name current."""
    first = sign_in(client)
    google.person = {"sub": "google-alice", "name": "Alice Smith"}
    second = sign_in(client)
    google.person = {"sub": "google-bob", "email": "alice@example.com"}
    other = sign_in(client)

    assert second["user"] == {"id": first["user"]["id"], "display_name": "Alice Smith"}
    # Same email as nothing else, different subject: a different, new account.
    assert other["user"]["id"] != first["user"]["id"]


def test_a_callback_in_another_browser_fails_and_uses_the_attempt(client: TestClient) -> None:
    """T9: the wrong binding fails, and then the right one can't reuse the attempt."""
    state, nonce, binding = start(client)
    finish = {"state": state, "code": f"code:{nonce}"}

    stolen = client.post("/auth/google/finish", json={**finish, "binding": "attacker"})
    afterwards = client.post("/auth/google/finish", json={**finish, "binding": binding})

    assert stolen.status_code == 400
    assert stolen.json()["error"]["code"] == "sign_in_failed"
    assert afterwards.status_code == 400
    # Control: a fresh attempt with its own binding signs in.
    assert sign_in(client)["session_token"]


def test_a_replayed_callback_fails(client: TestClient) -> None:
    """T9: a finished attempt can't be finished again."""
    state, nonce, binding = start(client)
    finish = {"state": state, "code": f"code:{nonce}", "binding": binding}

    assert client.post("/auth/google/finish", json=finish).status_code == 200
    replay = client.post("/auth/google/finish", json=finish)

    assert replay.status_code == 400


def test_an_unknown_state_fails(client: TestClient) -> None:
    """A state we never issued fails."""
    response = client.post(
        "/auth/google/finish",
        json={"state": "made-up", "code": "code:x", "binding": "y"},
    )

    assert response.status_code == 400


def test_an_old_attempt_fails(client: TestClient, database: Database) -> None:
    """An attempt older than 10 minutes fails."""
    state, nonce, binding = start(client)
    with database.session() as session:
        session.execute(
            update(SignInAttempt).values(created_at=datetime.now(UTC) - timedelta(minutes=11)),
        )

    response = client.post(
        "/auth/google/finish",
        json={"state": state, "code": f"code:{nonce}", "binding": binding},
    )

    assert response.status_code == 400


def test_old_attempts_are_removed_when_a_new_one_starts(
    client: TestClient,
    database: Database,
) -> None:
    """Attempts never finished don't pile up."""
    start(client)
    with database.session() as session:
        session.execute(
            update(SignInAttempt).values(created_at=datetime.now(UTC) - timedelta(hours=1)),
        )

    start(client)

    with database.session() as session:
        assert len(session.scalars(select(SignInAttempt)).all()) == 1


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "another-nonce"},
        {"iss": "https://evil.example"},
        {"aud": "someone-elses-client"},
        {"sub": ""},
    ],
)
def test_an_id_token_that_isnt_for_this_attempt_fails(
    client: TestClient,
    google: FakeGoogle,
    database: Database,
    claims: dict[str, Any],
) -> None:
    """A wrong nonce, issuer or audience, or no subject: no account and no session."""
    google.claims_override = claims
    state, nonce, binding = start(client)

    response = client.post(
        "/auth/google/finish",
        json={"state": state, "code": f"code:{nonce}", "binding": binding},
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Sign-in didn't complete; try again"
    with database.session() as session:
        assert session.scalars(select(User)).all() == []
        assert session.scalars(select(UserSession)).all() == []


def test_google_refusing_the_code_fails(client: TestClient, google: FakeGoogle) -> None:
    """Google refusing the code (expired, already used) is a failed sign-in."""
    google.status = 400
    state, nonce, binding = start(client)

    response = client.post(
        "/auth/google/finish",
        json={"state": state, "code": f"code:{nonce}", "binding": binding},
    )

    assert response.status_code == 400


def test_google_unreachable_is_503(client: TestClient, google: FakeGoogle) -> None:
    """Google being unreachable is 503, with nothing created."""
    google.unreachable = True
    state, nonce, binding = start(client)

    response = client.post(
        "/auth/google/finish",
        json={"state": state, "code": f"code:{nonce}", "binding": binding},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "google_unavailable"


def test_sign_in_is_503_when_not_configured(database: Database) -> None:
    """With no OAuth client configured, sign-in answers 503 and says why."""
    app.dependency_overrides[get_database] = lambda: database
    # Not a context manager: that would run the lifespan, which builds the real providers
    # and fails without their credentials. The route only reads app.state.google_sign_in.
    app.state.google_sign_in = None
    try:
        response = TestClient(app).post("/auth/google/start")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "sign_in_not_configured"


def test_settings_need_all_three_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sign-in is on only when the client ID, secret and redirect URI are all set."""
    monkeypatch.setenv(CLIENT_ID_ENV_VAR, "id")
    monkeypatch.setenv(CLIENT_SECRET_ENV_VAR, "secret")
    monkeypatch.delenv(REDIRECT_URI_ENV_VAR, raising=False)
    assert GoogleSettings.from_env() is None

    monkeypatch.setenv(REDIRECT_URI_ENV_VAR, "https://web.example.test/cb")
    assert GoogleSettings.from_env() == GoogleSettings(
        "id",
        "secret",
        "https://web.example.test/cb",
    )


# Sessions


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-real-token", "Basic dXNlcjpwYXNz", "Bearer ", "token-without-scheme"],
)
def test_a_bad_token_is_401(client: TestClient, header: str) -> None:
    """An unknown or malformed token is 401."""
    response = client.get("/auth/me", headers={"Authorization": header})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_signed_in"


def test_a_bad_token_doesnt_fall_back_to_the_dev_header(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With dev sign-in on, a bad bearer token still fails rather than using X-Dev-User."""
    monkeypatch.setenv(DEV_SIGN_IN_ENV_VAR, "enabled")

    response = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer stolen", "X-Dev-User": "alice"},
    )

    assert response.status_code == 401
    # Control: the dev header alone works where dev sign-in is on.
    assert client.get("/auth/me", headers={"X-Dev-User": "alice"}).status_code == 200


@pytest.mark.parametrize(
    "column",
    [
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"last_seen_at": datetime.now(UTC) - timedelta(hours=25)},
    ],
)
def test_an_expired_or_idle_session_is_401_and_removed(
    client: TestClient,
    database: Database,
    column: dict[str, datetime],
) -> None:
    """Past 7 days in all, or 24 hours unused, a session ends and its row is deleted."""
    token = sign_in(client)["session_token"]
    with database.session() as session:
        session.execute(update(UserSession).values(**column))

    response = client.get("/auth/me", headers=bearer(token))

    assert response.status_code == 401
    with database.session() as session:
        assert session.scalars(select(UserSession)).all() == []


def test_using_a_session_keeps_it_alive(client: TestClient, database: Database) -> None:
    """A session used after a while records the use, so it isn't idle."""
    token = sign_in(client)["session_token"]
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    with database.session() as session:
        session.execute(update(UserSession).values(last_seen_at=an_hour_ago))

    client.get("/auth/me", headers=bearer(token))

    with database.session() as session:
        seen = session.scalars(select(UserSession.last_seen_at)).one()
    assert seen.replace(tzinfo=UTC) > an_hour_ago + timedelta(minutes=59)


def test_sign_out_ends_the_session_and_its_agent_tokens(client: TestClient) -> None:
    """T7: after sign-out, the session and its agent tokens are 401."""
    token = sign_in(client)["session_token"]
    other = sign_in(client)["session_token"]
    agent = client.post("/auth/agent-token", headers=bearer(token)).json()["token"]

    assert client.post("/auth/sign-out", headers=bearer(token)).status_code == 204

    assert client.get("/auth/me", headers=bearer(token)).status_code == 401
    assert client.get("/auth/me", headers=bearer(agent)).status_code == 401
    # Control: the same person's other session still works.
    assert client.get("/auth/me", headers=bearer(other)).status_code == 200


def test_sign_out_everywhere_ends_every_session_of_that_user_only(
    client: TestClient,
    google: FakeGoogle,
) -> None:
    """Sign out everywhere ends all of Alice's sessions, and none of Bob's."""
    laptop = sign_in(client)["session_token"]
    phone = sign_in(client)["session_token"]
    google.person = {"sub": "google-bob", "name": "Bob"}
    bob = sign_in(client)["session_token"]

    assert client.post("/auth/sign-out-everywhere", headers=bearer(laptop)).status_code == 204

    assert client.get("/auth/me", headers=bearer(phone)).status_code == 401
    assert client.get("/auth/me", headers=bearer(bob)).status_code == 200


# Agent tokens (T14)


def test_an_agent_token_reads_and_proposes(client: TestClient) -> None:
    """An agent token can read and store a pending proposal, as the same user."""
    session = sign_in(client)
    agent = client.post("/auth/agent-token", headers=bearer(session["session_token"])).json()

    me = client.get("/auth/me", headers=bearer(agent["token"])).json()
    created = client.post("/proposals", json={"changes": [ISSUE]}, headers=bearer(agent["token"]))

    assert me == {"user_id": session["user"]["id"], "scope": "agent"}
    assert client.get("/auth/me", headers=bearer(agent["token"])).status_code == 200
    assert created.status_code == 201


def test_an_agent_token_cant_approve_reject_execute_or_settle(client: TestClient) -> None:
    """T14: the agent's token is 403 on approve, reject, execute and settling a change."""
    token = sign_in(client)["session_token"]
    agent = client.post("/auth/agent-token", headers=bearer(token)).json()["token"]
    proposal = client.post("/proposals", json={"changes": [ISSUE]}, headers=bearer(agent)).json()
    pid, approve = proposal["id"], {"changes_hash": proposal["changes_hash"]}

    refused = [
        client.post(f"/proposals/{pid}/approve", json=approve, headers=bearer(agent)),
        client.post(f"/proposals/{pid}/reject", headers=bearer(agent)),
        client.post(f"/proposals/{pid}/execute", headers=bearer(agent)),
        client.post(
            f"/proposals/{pid}/changes/0/resolve",
            json={"status": "failed"},
            headers=bearer(agent),
        ),
    ]

    assert [r.status_code for r in refused] == [403, 403, 403, 403]
    assert refused[0].json()["error"]["code"] == "permission_denied"
    assert client.get(f"/proposals/{pid}", headers=bearer(token)).json()["status"] == "pending"
    # Control: the person's own session approves it.
    approved = client.post(f"/proposals/{pid}/approve", json=approve, headers=bearer(token))
    assert approved.json()["status"] == "approved"


def test_an_agent_token_cant_make_tokens_or_sign_out(client: TestClient) -> None:
    """An agent token can't make more tokens, or end the person's sessions."""
    token = sign_in(client)["session_token"]
    agent = client.post("/auth/agent-token", headers=bearer(token)).json()["token"]

    for route in ("/auth/agent-token", "/auth/sign-out", "/auth/sign-out-everywhere"):
        assert client.post(route, headers=bearer(agent)).status_code == 403
    assert client.get("/auth/me", headers=bearer(token)).status_code == 200


def test_an_agent_token_lasts_five_minutes_and_never_past_its_session(
    client: TestClient,
    database: Database,
) -> None:
    """An agent token expires after 5 minutes, or when its session does, if sooner."""
    token = sign_in(client)["session_token"]
    agent = client.post("/auth/agent-token", headers=bearer(token)).json()
    issued = datetime.fromisoformat(agent["expires_at"])
    assert timedelta(minutes=4) < issued - datetime.now(UTC) <= timedelta(minutes=5)

    with database.session() as session:
        parent = session.scalars(select(UserSession).where(UserSession.scope == "session")).one()
        parent.expires_at = datetime.now(UTC) + timedelta(minutes=1)
    short = client.post("/auth/agent-token", headers=bearer(token)).json()
    assert datetime.fromisoformat(short["expires_at"]) - datetime.now(UTC) <= timedelta(minutes=1)

    with database.session() as session:
        session.execute(
            update(UserSession)
            .where(UserSession.scope == "agent")
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        )
    assert client.get("/auth/me", headers=bearer(agent["token"])).status_code == 401


def test_a_dev_user_can_make_an_agent_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where dev sign-in is on, a dev user gets agent tokens too, so tests can use them."""
    monkeypatch.setenv(DEV_SIGN_IN_ENV_VAR, "enabled")

    agent = client.post("/auth/agent-token", headers={"X-Dev-User": "alice"}).json()["token"]

    assert client.get("/auth/me", headers=bearer(agent)).json()["scope"] == "agent"
    assert client.post("/auth/sign-out", headers={"X-Dev-User": "alice"}).status_code == 204


def test_user_ids_are_never_taken_from_the_request(client: TestClient) -> None:
    """A user ID in the body is refused; the user comes only from the token."""
    token = sign_in(client)["session_token"]

    response = client.post(
        "/proposals",
        json={"changes": [ISSUE], "user_id": str(uuid.uuid4())},
        headers=bearer(token),
    )

    assert response.status_code == 400
