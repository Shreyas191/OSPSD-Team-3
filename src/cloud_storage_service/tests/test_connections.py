"""Provider connections over HTTP (#98-#101): connect, use, refuse, disconnect, reconnect.

Each provider is a :class:`FakeConnector` holding one in-memory account per person, so a
test can see whose Dropbox, board or calendar a write reached. Everything else is real:
the attempt store, the encrypted rows, the per-request clients and the routes. Trello's
list routes use the real ``TrelloConnector`` against a fake Trello.

Each refusal has a valid control next to it (threats T9, T10, T13 and T14).
"""

import hashlib
import json
import logging
import uuid
from base64 import urlsafe_b64encode
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cloud_storage_api.errors import NotFoundError
from course_autopilot_store import Database
from course_autopilot_store.models import (
    ConnectionAttempt,
    ExternalRecord,
    ProviderConnection,
    User,
)
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.pool import StaticPool

from cloud_storage_service.app import app
from cloud_storage_service.configuration import ConfigurationError
from cloud_storage_service.connections import (
    TOKEN_KEY_ENV_VAR,
    Connections,
    TokenBox,
    find_connections,
)
from cloud_storage_service.connectors import (
    Connector,
    ConnectorError,
    Grant,
    ReconnectNeededError,
    TrelloConnector,
    TrelloSettings,
)
from cloud_storage_service.database import get_database
from cloud_storage_service.dependencies import get_calendar, get_document_storage, get_issue_tracker
from cloud_storage_service.fake_verticals import (
    InMemoryCalendar,
    InMemoryDocumentStorage,
    InMemoryIssueTracker,
)
from cloud_storage_service.identity import DEV_SIGN_IN_ENV_VAR, create_session
from cloud_storage_service.proposals import COURSE_ROOT

COURSE = "CS-GY 6083"
ISSUE = {"op": "create_issue", "course": COURSE, "title": "[CS-GY 6083] Midterm, Dec 10"}
EVENT = {
    "op": "create_event",
    "course": COURSE,
    "title": "CS-GY 6083 midterm",
    "start": "2026-12-10T14:00:00-05:00",
    "end": "2026-12-10T15:30:00-05:00",
}


class Refused(Exception):  # noqa: N818 (what the fake provider says)
    """The fake provider no longer accepts this token."""


class _Refusing:
    """A client whose every call is refused, as a revoked token is."""

    def __getattr__(self, _name: str) -> Any:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            raise Refused

        return call


class FakeConnector:
    """One provider: an account per person, chosen by the code ``ok:<account>``."""

    def __init__(self, provider: str, title: str) -> None:
        """Start with no accounts and nothing revoked."""
        self.provider = provider
        self.title = title
        self.accounts: dict[str, Any] = {}
        self.refused: set[str] = set()
        self.revoked: list[str] = []
        self.challenges: dict[str, str] = {}
        self.revoke_fails = False
        self.unreachable = False
        self.setup = False

    def authorization_url(self, state: str, code_challenge: str) -> str:
        """Record the challenge sent for ``state``."""
        self.challenges[state] = code_challenge
        return f"https://{self.provider}.test/authorize?state={state}"

    def finish(self, code: str, code_verifier: str) -> Grant:
        """Check the PKCE pair, then grant the account named in the code."""
        if self.unreachable:
            raise ConnectorError(503, "provider_unavailable", f"{self.title} can't be reached")
        challenge = urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        assert challenge.rstrip(b"=").decode() in self.challenges.values()  # PKCE pairs up
        if not code.startswith("ok:"):
            raise ConnectorError(400, "connect_failed", f"{self.title} didn't connect")
        account = code.removeprefix("ok:")
        secret = {"token": token_for(self.provider, account), "account": account}
        return Grant(secret=secret, account=account, scopes="all")

    def needs_setup(self, _secret: Mapping[str, Any]) -> bool:
        """Need setup only when the test says so."""
        return self.setup

    def build(self, secret: dict[str, Any], save: Any, _cleanup: ExitStack) -> object:
        """Return the account's client; a refused token gives one that refuses."""
        if secret["token"] in self.refused:
            return _Refusing()
        if secret.get("stale"):
            raise ReconnectNeededError
        save({**secret, "used": True})
        account = secret["account"]
        if account not in self.accounts:
            fresh = {
                "dropbox": InMemoryDocumentStorage,
                "trello": InMemoryIssueTracker,
                "google_calendar": InMemoryCalendar,
            }[self.provider]
            self.accounts[account] = fresh()
        return self.accounts[account]

    def is_auth_failure(self, error: BaseException) -> bool:
        """Recognize the fake's refusal."""
        return isinstance(error, Refused)

    def revoke(self, secret: Mapping[str, Any]) -> None:
        """Record the revoked token, or fail when the test says so."""
        if self.revoke_fails:
            raise ConnectorError(503, "provider_unavailable", "unreachable")
        self.revoked.append(str(secret["token"]))


def token_for(provider: str, account: str) -> str:
    """Return the fake's token for an account, easy to search for in logs and responses."""
    return f"SECRET-{provider}-{account}-token"


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
def box() -> TokenBox:
    """Provide a token box with a fresh key."""
    return TokenBox(Fernet.generate_key().decode())


@pytest.fixture
def fakes() -> dict[str, FakeConnector]:
    """Provide a fake connector for each provider."""
    return {
        "dropbox": FakeConnector("dropbox", "Dropbox"),
        "google_calendar": FakeConnector("google_calendar", "Google Calendar"),
        "trello": FakeConnector("trello", "Trello"),
    }


class Setup:
    """What the app uses for connections; a test can swap it (or set it to None)."""

    def __init__(self, connections: Connections | None) -> None:
        """Hold the current setup."""
        self.connections = connections


@pytest.fixture
def setup(box: TokenBox, fakes: dict[str, FakeConnector]) -> Setup:
    """Provide the app's connections: the box and the fake connectors."""
    connectors: dict[str, Connector] = dict(fakes)
    return Setup(Connections(box, connectors))


@pytest.fixture
def shared() -> dict[str, Any]:
    """Provide the service's shared clients (what a development user runs on)."""
    return {
        "storage": InMemoryDocumentStorage(),
        "issues": InMemoryIssueTracker(),
        "calendar": InMemoryCalendar(),
    }


@pytest.fixture
def client(
    database: Database,
    setup: Setup,
    shared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Serve the API app with the fake connectors; dev sign-in off unless a test turns it on."""
    monkeypatch.delenv(DEV_SIGN_IN_ENV_VAR, raising=False)
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[find_connections] = lambda: setup.connections
    app.dependency_overrides[get_document_storage] = lambda: shared["storage"]
    app.dependency_overrides[get_issue_tracker] = lambda: shared["issues"]
    app.dependency_overrides[get_calendar] = lambda: shared["calendar"]
    yield TestClient(app)
    app.dependency_overrides.clear()


def person(database: Database, name: str) -> dict[str, str]:
    """Create a Google-signed-in user with a session; return their auth header."""
    with database.session() as session:
        user = User(auth_provider="google", auth_subject=f"google-{name}", display_name=name)
        session.add(user)
        session.flush()
        token, _ = create_session(session, user.id)
    return {"Authorization": f"Bearer {token}"}


def user_id(client: TestClient, who: dict[str, str]) -> uuid.UUID:
    """Return the user ID behind an auth header."""
    return uuid.UUID(client.get("/auth/me", headers=who).json()["user_id"])


def agent_header(database: Database, client: TestClient, who: dict[str, str]) -> dict[str, str]:
    """Make an agent token for this person's session."""
    token = client.post("/auth/agent-token", headers=who).json()["token"]
    assert database  # the token is stored in the same database
    return {"Authorization": f"Bearer {token}"}


def connect(
    client: TestClient,
    who: dict[str, str],
    provider: str,
    account: str,
) -> dict[str, Any]:
    """Connect ``provider`` from start to finish as ``account``; return the connection."""
    url = client.post(f"/connections/{provider}/start", headers=who).json()["authorization_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    response = client.post(
        f"/connections/{provider}/finish",
        json={"state": state, "code": f"ok:{account}"},
        headers=who,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def statuses(client: TestClient, who: dict[str, str]) -> dict[str, str]:
    """Return each provider's connection status."""
    return {c["provider"]: c["status"] for c in client.get("/connections", headers=who).json()}


def run(client: TestClient, who: dict[str, str], *changes: dict[str, Any]) -> dict[str, Any]:
    """Propose, approve and execute ``changes``; return the executed proposal."""
    proposal = client.post("/proposals", json={"changes": list(changes)}, headers=who).json()
    approved = client.post(
        f"/proposals/{proposal['id']}/approve",
        json={"changes_hash": proposal["changes_hash"]},
        headers=who,
    )
    assert approved.status_code == 200, approved.text
    response = client.post(f"/proposals/{proposal['id']}/execute", headers=who)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# Listing, connecting


def test_nothing_is_connected_at_first(client: TestClient, database: Database) -> None:
    """Each provider is listed, can be connected here, and isn't connected yet."""
    body = client.get("/connections", headers=person(database, "alice")).json()

    assert [(c["provider"], c["status"], c["available"]) for c in body] == [
        ("dropbox", "not_connected", True),
        ("google_calendar", "not_connected", True),
        ("trello", "not_connected", True),
    ]


def test_connecting_stores_the_secret_encrypted_and_never_returns_it(
    client: TestClient,
    database: Database,
) -> None:
    """A finished connection shows the account; the row holds only ciphertext."""
    alice = person(database, "alice")

    body = connect(client, alice, "dropbox", "alice@example.test")

    assert body["status"] == "connected"
    assert body["account"] == "alice@example.test"
    assert "token" not in json.dumps(body).lower()
    with database.session() as session:
        row = session.scalars(select(ProviderConnection)).one()
        assert token_for("dropbox", "alice@example.test").encode() not in row.encrypted_credentials
    assert statuses(client, alice)["dropbox"] == "connected"


def test_the_start_url_carries_a_pkce_challenge_the_finish_matches(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
) -> None:
    """The verifier kept for the attempt is the one whose challenge went to the provider."""
    connect(client, person(database, "alice"), "trello", "alice")

    assert len(fakes["trello"].challenges) == 1  # finish asserted the pair matched


# T9: the attempt belongs to the session that started it, and is used once


def test_a_callback_finished_by_another_session_connects_nothing(
    client: TestClient,
    database: Database,
) -> None:
    """T9: Bob finishing Alice's attempt fails, and the attempt is then used up."""
    alice, bob = person(database, "alice"), person(database, "bob")
    url = client.post("/connections/dropbox/start", headers=alice).json()["authorization_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    finish = {"state": state, "code": "ok:mallory"}

    stolen = client.post("/connections/dropbox/finish", json=finish, headers=bob)
    retried = client.post("/connections/dropbox/finish", json=finish, headers=alice)

    assert stolen.status_code == 400
    assert stolen.json()["error"]["code"] == "connect_failed"
    assert retried.status_code == 400  # used once, even by a failed check
    assert statuses(client, alice)["dropbox"] == statuses(client, bob)["dropbox"] == "not_connected"
    # Control: a fresh attempt finished by the session that started it connects.
    assert connect(client, alice, "dropbox", "alice")["status"] == "connected"


def test_a_state_is_used_once(client: TestClient, database: Database) -> None:
    """Replaying a finished callback fails and leaves the first connection as it was."""
    alice = person(database, "alice")
    url = client.post("/connections/trello/start", headers=alice).json()["authorization_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    first = client.post(
        "/connections/trello/finish",
        json={"state": state, "code": "ok:alice"},
        headers=alice,
    )

    replay = client.post(
        "/connections/trello/finish",
        json={"state": state, "code": "ok:mallory"},
        headers=alice,
    )

    assert first.status_code == 200
    assert replay.status_code == 400
    assert client.get("/connections", headers=alice).json()[2]["account"] == "alice"


@pytest.mark.parametrize("problem", ["unknown", "other_provider", "old"])
def test_a_bad_attempt_connects_nothing(
    client: TestClient,
    database: Database,
    problem: str,
) -> None:
    """An unknown state, one started for another provider, or an old one, fails."""
    alice = person(database, "alice")
    url = client.post("/connections/dropbox/start", headers=alice).json()["authorization_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    provider = "dropbox"
    if problem == "unknown":
        state = "not-a-state"
    elif problem == "other_provider":
        provider = "trello"
    else:
        with database.session() as session:
            session.execute(
                update(ConnectionAttempt).values(
                    created_at=datetime.now(UTC) - timedelta(minutes=11),
                ),
            )

    response = client.post(
        f"/connections/{provider}/finish",
        json={"state": state, "code": "ok:alice"},
        headers=alice,
    )

    assert response.status_code == 400
    assert set(statuses(client, alice).values()) == {"not_connected"}


def test_old_attempts_are_removed_when_a_new_one_starts(
    client: TestClient,
    database: Database,
) -> None:
    """Attempts that can no longer be used don't pile up."""
    alice = person(database, "alice")
    client.post("/connections/dropbox/start", headers=alice)
    with database.session() as session:
        session.execute(
            update(ConnectionAttempt).values(created_at=datetime.now(UTC) - timedelta(hours=1)),
        )

    client.post("/connections/dropbox/start", headers=alice)

    with database.session() as session:
        assert len(session.scalars(select(ConnectionAttempt)).all()) == 1


@pytest.mark.parametrize(
    ("code", "status", "error"),
    [("refused", 400, "connect_failed"), ("ok:alice", 503, "provider_unavailable")],
)
def test_a_provider_that_refuses_or_is_down_connects_nothing(  # noqa: PLR0913
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
    code: str,
    status: int,
    error: str,
) -> None:
    """The provider's answer decides the status; nothing is stored."""
    alice = person(database, "alice")
    fakes["dropbox"].unreachable = status == 503
    url = client.post("/connections/dropbox/start", headers=alice).json()["authorization_url"]
    state = parse_qs(urlparse(url).query)["state"][0]

    response = client.post(
        "/connections/dropbox/finish",
        json={"state": state, "code": code},
        headers=alice,
    )

    assert response.status_code == status
    assert response.json()["error"]["code"] == error
    assert statuses(client, alice)["dropbox"] == "not_connected"


# T14: only the person's own session


def test_an_agent_token_cant_touch_connections(client: TestClient, database: Database) -> None:
    """T14: every connection route refuses an agent token (control: the session works)."""
    alice = person(database, "alice")
    agent = agent_header(database, client, alice)

    calls = [
        client.get("/connections", headers=agent),
        client.post("/connections/dropbox/start", headers=agent),
        client.post("/connections/dropbox/finish", json={"state": "s", "code": "c"}, headers=agent),
        client.delete("/connections/dropbox", headers=agent),
        client.get("/connections/trello/lists", headers=agent),
        client.put("/connections/trello/list", json={"list_id": "l"}, headers=agent),
    ]

    assert [r.status_code for r in calls] == [403] * len(calls)
    assert client.get("/connections", headers=alice).status_code == 200


def test_connections_need_sign_in(client: TestClient) -> None:
    """No session is 401."""
    assert client.get("/connections").status_code == 401


# Using a connection: the person's own account, never a fallback (T13)


def test_a_product_route_needs_the_connection(client: TestClient, database: Database) -> None:
    """Not connected is 409 ``not_connected``; once connected, the route works."""
    alice = person(database, "alice")

    before = client.get("/courses", headers=alice)

    assert before.status_code == 409
    assert before.json()["error"] == {"code": "not_connected", "message": "Connect Dropbox first"}
    connect(client, alice, "dropbox", "alice")
    assert client.get("/courses", headers=alice).status_code == 200


def test_reads_reach_only_the_persons_own_dropbox(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
    shared: dict[str, Any],
) -> None:
    """Alice sees her course folder; Bob, connected to his own Dropbox, doesn't."""
    alice, bob = person(database, "alice"), person(database, "bob")
    connect(client, alice, "dropbox", "alice")
    connect(client, bob, "dropbox", "bob")
    client.get("/courses", headers=alice)  # builds Alice's account in the fake
    storage: InMemoryDocumentStorage = fakes["dropbox"].accounts["alice"]
    storage.create(f"{COURSE_ROOT}/{COURSE}/syllabus.md", b"# Syllabus")

    mine = client.get("/courses", headers=alice).json()
    theirs = client.get("/courses", headers=bob).json()

    assert [c["course"] for c in mine] == [COURSE]
    assert theirs == []
    with pytest.raises(NotFoundError):  # our test account was never used
        shared["storage"].list_files(COURSE_ROOT)


def test_two_peoples_cards_land_on_their_own_boards(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
    shared: dict[str, Any],
) -> None:
    """#101: each person's approved card goes to their own Trello, none to ours."""
    alice, bob = person(database, "alice"), person(database, "bob")
    connect(client, alice, "trello", "alice")
    connect(client, bob, "trello", "bob")

    first = run(client, alice, ISSUE)
    second = run(client, bob, {**ISSUE, "title": "[CS-GY 6083] Bob's card"})

    assert first["status"] == second["status"] == "done"
    assert [i.title for i in fakes["trello"].accounts["alice"].list_issues(limit=10)] == [
        ISSUE["title"],
    ]
    assert [i.title for i in fakes["trello"].accounts["bob"].list_issues(limit=10)] == [
        "[CS-GY 6083] Bob's card",
    ]
    assert shared["issues"].list_issues(limit=10) == []


def test_events_land_in_the_persons_own_calendar(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
) -> None:
    """#100: an approved event is created in the connected calendar."""
    alice = person(database, "alice")
    connect(client, alice, "google_calendar", "alice@example.test")

    body = run(client, alice, EVENT)

    assert body["status"] == "done"
    with database.session() as session:
        event_id = session.scalars(select(ExternalRecord.provider_record_id)).one()
    assert (
        fakes["google_calendar"].accounts["alice@example.test"].get(event_id).title
        == (EVENT["title"])
    )


def test_executing_without_a_connection_fails_that_change_and_writes_nothing(
    client: TestClient,
    database: Database,
    shared: dict[str, Any],
) -> None:
    """T13: no Trello connection → the change fails with a message; the proposal can retry."""
    alice = person(database, "alice")

    body = run(client, alice, ISSUE)

    assert body["status"] == "failed"
    assert body["outcomes"][0]["status"] == "failed"
    assert body["outcomes"][0]["detail"] == "Connect Trello first"
    assert shared["issues"].list_issues(limit=10) == []
    # Control: after connecting, retrying the same proposal runs it.
    connect(client, alice, "trello", "alice")
    retried = client.post(f"/proposals/{body['id']}/execute", headers=alice).json()
    assert retried["status"] == "done"


def test_a_development_user_runs_on_the_shared_clients(
    client: TestClient,
    database: Database,
    shared: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dev user has no connections and uses the service's shared (test) clients."""
    monkeypatch.setenv(DEV_SIGN_IN_ENV_VAR, "enabled")
    dev = {"X-Dev-User": "tester"}

    body = run(client, dev, ISSUE)
    start = client.post("/connections/trello/start", headers=dev)

    assert body["status"] == "done"
    assert [i.title for i in shared["issues"].list_issues(limit=10)] == [ISSUE["title"]]
    assert start.status_code == 409


# A connection the provider refuses (revoked, or expired)


def test_a_refused_token_asks_to_reconnect_and_reconnecting_fixes_it(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
) -> None:
    """T13: a revoked token → 409 reconnect, ``needs_attention``; reconnecting works again."""
    alice = person(database, "alice")
    connect(client, alice, "dropbox", "alice")
    fakes["dropbox"].refused.add(token_for("dropbox", "alice"))

    refused = client.get("/courses", headers=alice)
    again = client.get("/courses", headers=alice)

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "reconnect_needed"
    assert refused.json()["error"]["message"] == "Reconnect Dropbox, then try again"
    assert statuses(client, alice)["dropbox"] == "needs_attention"
    assert again.json()["error"]["code"] == "reconnect_needed"  # no provider call is tried
    fakes["dropbox"].refused.clear()
    assert connect(client, alice, "dropbox", "alice")["status"] == "connected"
    assert client.get("/courses", headers=alice).status_code == 200


def test_a_refused_token_during_execution_fails_the_change(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
) -> None:
    """A write the provider refuses is ``failed`` with the reconnect message, not stuck."""
    alice = person(database, "alice")
    connect(client, alice, "google_calendar", "alice")
    fakes["google_calendar"].refused.add(token_for("google_calendar", "alice"))

    body = run(client, alice, EVENT)

    assert body["status"] == "failed"
    assert body["outcomes"][0]["detail"] == "Reconnect Google Calendar, then try again"
    assert statuses(client, alice)["google_calendar"] == "needs_attention"


def test_a_refused_refresh_marks_the_connection(
    client: TestClient,
    database: Database,
    box: TokenBox,
) -> None:
    """A connector that can't refresh the secret (invalid_grant) → reconnect."""
    alice = person(database, "alice")
    connect(client, alice, "google_calendar", "alice")
    uid = user_id(client, alice)
    with database.session() as session:
        row = session.scalars(select(ProviderConnection)).one()
        secret = box.open(uid, "google_calendar", row.encrypted_credentials)
        row.encrypted_credentials = box.seal(uid, "google_calendar", {**secret, "stale": True})

    body = run(client, alice, EVENT)

    assert body["outcomes"][0]["detail"] == "Reconnect Google Calendar, then try again"
    assert statuses(client, alice)["google_calendar"] == "needs_attention"


def test_a_refreshed_secret_is_saved_back(
    client: TestClient,
    database: Database,
    box: TokenBox,
) -> None:
    """What a connector refreshes during a request is stored, encrypted, for the next one."""
    alice = person(database, "alice")
    connect(client, alice, "dropbox", "alice")

    client.get("/courses", headers=alice)

    with database.session() as session:
        row = session.scalars(select(ProviderConnection)).one()
        assert box.open(user_id(client, alice), "dropbox", row.encrypted_credentials)["used"]


def test_a_secret_copied_from_another_row_is_refused(
    client: TestClient,
    database: Database,
) -> None:
    """Ciphertext moved into another user's row doesn't decrypt there → reconnect."""
    alice, bob = person(database, "alice"), person(database, "bob")
    connect(client, alice, "dropbox", "alice")
    connect(client, bob, "dropbox", "bob")
    with database.session() as session:
        rows = {r.account_label: r for r in session.scalars(select(ProviderConnection))}
        rows["bob"].encrypted_credentials = rows["alice"].encrypted_credentials

    stolen = client.get("/courses", headers=bob)

    assert stolen.json()["error"]["code"] == "reconnect_needed"
    assert client.get("/courses", headers=alice).status_code == 200  # control


# Disconnecting


def test_disconnect_deletes_then_revokes_and_reconnect_works(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
) -> None:
    """#98: disconnect removes our secret and revokes it; connecting again works."""
    alice = person(database, "alice")
    connect(client, alice, "trello", "alice")

    response = client.delete("/connections/trello", headers=alice)

    assert response.status_code == 204
    assert fakes["trello"].revoked == [token_for("trello", "alice")]
    assert statuses(client, alice)["trello"] == "not_connected"
    assert run(client, alice, ISSUE)["outcomes"][0]["detail"] == "Connect Trello first"
    connect(client, alice, "trello", "alice")
    assert statuses(client, alice)["trello"] == "connected"


def test_disconnecting_twice_or_when_revoke_fails_still_succeeds(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Our copy is gone either way; a failed revoke is logged without the token."""
    alice = person(database, "alice")
    connect(client, alice, "dropbox", "alice")
    fakes["dropbox"].revoke_fails = True

    with caplog.at_level(logging.WARNING):
        first = client.delete("/connections/dropbox", headers=alice)
    second = client.delete("/connections/dropbox", headers=alice)

    assert first.status_code == second.status_code == 204
    assert "Revoking a dropbox connection at the provider failed" in caplog.text
    assert token_for("dropbox", "alice") not in caplog.text
    assert statuses(client, alice)["dropbox"] == "not_connected"


def test_disconnect_leaves_the_other_providers(
    client: TestClient,
    database: Database,
) -> None:
    """Disconnecting one provider leaves the others connected."""
    alice = person(database, "alice")
    connect(client, alice, "dropbox", "alice")
    connect(client, alice, "trello", "alice")

    client.delete("/connections/dropbox", headers=alice)

    assert statuses(client, alice) == {
        "dropbox": "not_connected",
        "google_calendar": "not_connected",
        "trello": "connected",
    }


# T10: tokens never in a response or a log line


def test_no_token_reaches_a_response_or_a_log_line(
    client: TestClient,
    database: Database,
    fakes: dict[str, FakeConnector],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T10: a whole connect, use, refuse and disconnect cycle never shows a token."""
    alice = person(database, "alice")
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        texts.extend(
            json.dumps(connect(client, alice, provider, "alice"))
            for provider in ("dropbox", "trello", "google_calendar")
        )
        texts.append(json.dumps(run(client, alice, ISSUE, EVENT)))
        texts.append(client.get("/courses", headers=alice).text)
        texts.append(client.get("/connections", headers=alice).text)
        fakes["dropbox"].refused.add(token_for("dropbox", "alice"))
        texts.append(client.get("/courses", headers=alice).text)
        texts.extend(
            client.delete(f"/connections/{provider}", headers=alice).text
            for provider in ("dropbox", "trello", "google_calendar")
        )

    for provider in ("dropbox", "trello", "google_calendar"):
        token = token_for(provider, "alice")
        assert all(token not in text for text in texts)
        assert token not in caplog.text
    assert "list_connections" in caplog.text  # control: the request log lines exist


# Not configured


def test_without_a_key_connections_are_off(
    client: TestClient,
    database: Database,
    setup: Setup,
) -> None:
    """No token encryption key: connection and product routes are 503, not a fallback."""
    setup.connections = None
    alice = person(database, "alice")

    listed = client.get("/connections", headers=alice)
    courses = client.get("/courses", headers=alice)

    assert listed.status_code == courses.status_code == 503
    assert (
        listed.json()["error"]["code"]
        == courses.json()["error"]["code"]
        == ("connections_not_configured")
    )


def test_a_provider_without_settings_cant_be_connected(
    client: TestClient,
    database: Database,
    setup: Setup,
    box: TokenBox,
    fakes: dict[str, FakeConnector],
) -> None:
    """A provider with no connector here is listed as unavailable; starting it is 503."""
    setup.connections = Connections(box, {"dropbox": fakes["dropbox"]})
    alice = person(database, "alice")

    listed = {
        c["provider"]: c["available"] for c in client.get("/connections", headers=alice).json()
    }
    start = client.post("/connections/trello/start", headers=alice)
    finish = client.post(
        "/connections/trello/finish",
        json={"state": "s", "code": "c"},
        headers=alice,
    )

    assert listed == {"dropbox": True, "google_calendar": False, "trello": False}
    assert start.status_code == finish.status_code == 503
    assert start.json()["error"]["code"] == "connection_not_configured"
    assert run(client, alice, ISSUE)["outcomes"][0]["detail"] == "Connect Trello first"


def test_a_bad_key_stops_the_service_at_startup() -> None:
    """A key that isn't a Fernet key is a configuration error naming the setting only."""
    with pytest.raises(ConfigurationError, match=TOKEN_KEY_ENV_VAR) as raised:
        TokenBox("not-a-key")

    assert "not-a-key" not in str(raised.value)


def test_the_key_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``TOKEN_ENCRYPTION_KEY`` unset → no box; set → a box that round-trips."""
    monkeypatch.delenv(TOKEN_KEY_ENV_VAR, raising=False)
    assert TokenBox.from_env() is None
    monkeypatch.setenv(TOKEN_KEY_ENV_VAR, Fernet.generate_key().decode())
    box = TokenBox.from_env()
    assert box is not None
    uid = uuid.uuid4()
    assert box.open(uid, "trello", box.seal(uid, "trello", {"token": "t"})) == {"token": "t"}
    with pytest.raises(ValueError, match="another user"):
        box.open(uuid.uuid4(), "trello", box.seal(uid, "trello", {"token": "t"}))
    with pytest.raises(ValueError, match="decrypted"):
        TokenBox(Fernet.generate_key().decode()).open(uid, "trello", box.seal(uid, "trello", {}))


# Trello's list (#101), with the real Trello connector and a fake Trello


class FakeTrello:
    """Trello's API: one member with one board of two lists."""

    def __init__(self) -> None:
        """Accept one token."""
        self.token = "trello-token-alice"
        self.deleted: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer members/me, boards and token deletion, for the accepted token only."""
        assert "token=" not in str(request.url.query)  # the token is in the header only
        if f'oauth_token="{self.token}"' not in request.headers.get("Authorization", ""):
            return httpx.Response(401, text="invalid token")
        path = request.url.path
        if path == "/1/members/me":
            return httpx.Response(200, json={"username": "alice", "fullName": "Alice A"})
        if path == "/1/members/me/boards":
            lists = [{"id": "list-todo", "name": "To do"}, {"id": "list-done", "name": "Done"}]
            return httpx.Response(200, json=[{"name": "School", "lists": lists}])
        if request.method == "DELETE" and path.startswith("/1/tokens/"):
            self.deleted.append(path.removeprefix("/1/tokens/"))
            return httpx.Response(200, json={})
        return httpx.Response(404)


@pytest.fixture
def trello(setup: Setup, box: TokenBox, fakes: dict[str, FakeConnector]) -> FakeTrello:
    """Use the real Trello connector against a fake Trello."""
    fake = FakeTrello()
    connector = TrelloConnector(
        TrelloSettings("key-123", "https://web.example.test/connections/trello"),
        httpx.Client(transport=httpx.MockTransport(fake.handle)),
    )
    setup.connections = Connections(box, {**fakes, "trello": connector})
    return fake


def connect_trello(client: TestClient, who: dict[str, str], token: str) -> httpx.Response:
    """Start a Trello connection and finish it with ``token`` from the return URL."""
    url = client.post("/connections/trello/start", headers=who).json()["authorization_url"]
    return_url = parse_qs(urlparse(url).query)["return_url"][0]
    state = parse_qs(urlparse(return_url).query)["state"][0]
    response: httpx.Response = client.post(
        "/connections/trello/finish",
        json={"state": state, "code": token},
        headers=who,
    )
    return response


def test_trello_needs_a_list_before_cards_can_be_made(
    client: TestClient,
    database: Database,
    trello: FakeTrello,
) -> None:
    """Connected Trello is ``setup_needed`` until a list is chosen; then cards work."""
    alice = person(database, "alice")

    connected = connect_trello(client, alice, trello.token).json()
    before = run(client, alice, ISSUE)
    lists = client.get("/connections/trello/lists", headers=alice).json()
    chosen = client.put("/connections/trello/list", json={"list_id": "list-todo"}, headers=alice)

    assert connected["status"] == "setup_needed"
    assert connected["account"] == "Alice A (@alice)"
    assert before["outcomes"][0]["detail"] == (
        "Choose the Trello list for your cards on the connections page"
    )
    assert lists == [
        {"board": "School", "id": "list-todo", "name": "To do"},
        {"board": "School", "id": "list-done", "name": "Done"},
    ]
    assert chosen.status_code == 200
    assert chosen.json()["status"] == "connected"
    assert statuses(client, alice)["trello"] == "connected"


def test_only_the_persons_own_lists_can_be_chosen(
    client: TestClient,
    database: Database,
    trello: FakeTrello,
) -> None:
    """A list ID not on the person's boards is 400 and changes nothing."""
    alice = person(database, "alice")
    connect_trello(client, alice, trello.token)

    response = client.put(
        "/connections/trello/list",
        json={"list_id": "someone-else"},
        headers=alice,
    )

    assert response.status_code == 400
    assert statuses(client, alice)["trello"] == "setup_needed"


def test_a_token_trello_refuses_doesnt_connect(
    client: TestClient,
    database: Database,
    trello: FakeTrello,
) -> None:
    """A made-up token is 400 (control: Trello's real token connects)."""
    alice = person(database, "alice")

    assert connect_trello(client, alice, "made-up").status_code == 400
    assert connect_trello(client, alice, trello.token).status_code == 200


def test_trello_lists_need_a_connection_that_trello_still_accepts(
    client: TestClient,
    database: Database,
    trello: FakeTrello,
) -> None:
    """No connection → 409 not_connected; a revoked one → 409 reconnect, marked."""
    alice = person(database, "alice")
    assert client.get("/connections/trello/lists", headers=alice).json()["error"]["code"] == (
        "not_connected"
    )
    connect_trello(client, alice, trello.token)
    trello.token = "rotated"

    lists = client.get("/connections/trello/lists", headers=alice)
    choose = client.put("/connections/trello/list", json={"list_id": "list-todo"}, headers=alice)

    assert lists.json()["error"]["code"] == choose.json()["error"]["code"] == "reconnect_needed"
    assert statuses(client, alice)["trello"] == "needs_attention"


def test_disconnecting_trello_deletes_the_token_there(
    client: TestClient,
    database: Database,
    trello: FakeTrello,
) -> None:
    """Disconnect revokes the token at Trello."""
    alice = person(database, "alice")
    connect_trello(client, alice, trello.token)

    client.delete("/connections/trello", headers=alice)

    assert trello.deleted == [trello.token]
