"""Proposals (#89) and their execution (#90), over HTTP, on controlled implementations.

Two users, ``alice`` and ``bob``. Each rule from DESIGN.md (P1-P3) and the threat table
(T2-T4, T6, T14) has a test with a valid control request, so rejecting everything can't pass.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from calendar_contract import ErrorCode as CalendarErrorCode
from course_autopilot_store import Database
from course_autopilot_store.models import ExternalRecord
from dropbox_client_impl import DropboxDocumentStorage
from fastapi.testclient import TestClient
from issue_tracker_contract.errors import ErrorCode as IssueErrorCode
from issue_tracker_contract.errors import ServiceError
from issue_tracker_contract.models import Issue
from sqlalchemy import create_engine, event, select
from sqlalchemy.pool import StaticPool
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app
from cloud_storage_service.database import get_database
from cloud_storage_service.dependencies import get_calendar, get_document_storage, get_issue_tracker
from cloud_storage_service.fake_verticals import (
    FailingCalendar,
    FailingIssueTracker,
    InMemoryCalendar,
    InMemoryIssueTracker,
)
from cloud_storage_service.identity import DEV_SIGN_IN_ENV_VAR
from cloud_storage_service.proposals import changes_hash

ALICE = {"X-Dev-User": "alice"}
BOB = {"X-Dev-User": "bob"}
COURSE = "CS-GY 6083"
EVENT = {
    "op": "create_event",
    "course": COURSE,
    "title": "Midterm",
    "start": "2026-12-10T14:00:00-05:00",
    "end": "2026-12-10T15:30:00-05:00",
}
ISSUE = {"op": "create_issue", "course": COURSE, "title": "[CS-GY 6083] Midterm, Dec 10"}
DOC = {
    "op": "write_document",
    "course": COURSE,
    "name": "course plan.md",
    "content": "# CS-GY 6083\n\n- Midterm: Dec 10\n",
}
PLAN = f"/Course Autopilot/{COURSE}/course plan.md"
NEW_PLAN = "# CS-GY 6083\n\n- Midterm: Dec 11 (moved from Dec 10)\n"


class Verticals:
    """Controlled implementations of the three contracts; tests swap them."""

    def __init__(self) -> None:
        """Start with working, empty implementations."""
        self.client = FakeStorageClient()
        self.storage = DropboxDocumentStorage(self.client)
        self.issues: Any = InMemoryIssueTracker()
        self.calendar: Any = InMemoryCalendar()


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
def v(database: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[Verticals]:
    """Wire the app to the database and the controlled verticals, with dev sign-in on."""
    monkeypatch.setenv(DEV_SIGN_IN_ENV_VAR, "enabled")
    monkeypatch.delenv("K_SERVICE", raising=False)
    verticals = Verticals()
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_document_storage] = lambda: verticals.storage
    app.dependency_overrides[get_issue_tracker] = lambda: verticals.issues
    app.dependency_overrides[get_calendar] = lambda: verticals.calendar
    yield verticals
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return a client that doesn't run the lifespan (which would build real providers)."""
    return TestClient(app)


def propose(
    http: TestClient,
    *changes: dict[str, Any],
    who: dict[str, str] = ALICE,
) -> dict[str, Any]:
    """Create a proposal and return its body."""
    response = http.post("/proposals", json={"changes": list(changes)}, headers=who)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def approve(http: TestClient, proposal: dict[str, Any], who: dict[str, str] = ALICE) -> None:
    """Approve the proposal's current content."""
    response = http.post(
        f"/proposals/{proposal['id']}/approve",
        json={"changes_hash": proposal["changes_hash"]},
        headers=who,
    )
    assert response.status_code == 200, response.text


def execute(
    http: TestClient,
    proposal: dict[str, Any],
    who: dict[str, str] = ALICE,
) -> dict[str, Any]:
    """Execute and return the body (any status)."""
    body: dict[str, Any] = http.post(f"/proposals/{proposal['id']}/execute", headers=who).json()
    return body


def statuses(body: dict[str, Any]) -> list[str]:
    """Each recorded outcome's status, in change order."""
    return [o["status"] for o in body["outcomes"]]


# ----- #89: creating, reading, approving, rejecting -----


def test_a_proposal_is_stored_pending_and_writes_nothing(v: Verticals, http: TestClient) -> None:
    """Proposing changes nothing in any provider."""
    body = propose(http, EVENT, ISSUE, DOC)

    assert body["status"] == "pending"
    assert body["changes_hash"] == changes_hash(body["changes"])
    assert body["outcomes"] == []
    assert v.issues.list_issues(limit=10) == []
    assert v.client.entries == {}


def test_the_hash_identifies_the_exact_content(v: Verticals, http: TestClient) -> None:
    """Same content, same hash; any difference, a different hash."""
    first, same = propose(http, EVENT), propose(http, EVENT)
    other = propose(http, {**EVENT, "title": "Midterm (room 2)"})

    assert first["changes_hash"] == same["changes_hash"] != other["changes_hash"]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({**EVENT, "end": "2026-12-10T13:00:00-05:00"}, "ends before it starts"),
        ({**EVENT, "start": "2026-12-10T14:00:00"}, "time without an offset"),
        ({**ISSUE, "title": "x" * 300}, "title longer than the issue contract allows"),
        ({**ISSUE, "course": "CS/6083"}, "course name with a slash"),
        ({**DOC, "name": "../escape.md"}, "document path traversal"),
        ({**DOC, "name": "plan.exe"}, "document that isn't Markdown"),
        ({"op": "delete_everything", "course": COURSE}, "unknown operation"),
        ({**ISSUE, "user_id": "someone-else"}, "extra field, such as a user ID"),
    ],
)
def test_invalid_changes_are_refused_before_anything_is_stored(
    v: Verticals,
    http: TestClient,
    change: dict[str, Any],
    reason: str,
) -> None:
    """400 in the shared error body (control: the valid change is accepted)."""
    response = http.post("/proposals", json={"changes": [change]}, headers=ALICE)

    assert response.status_code == 400, reason
    assert response.json()["error"]["code"] == "invalid_request"
    assert http.post("/proposals", json={"changes": [ISSUE]}, headers=ALICE).status_code == 201


def test_a_change_cant_target_a_record_this_account_didnt_create(
    v: Verticals,
    http: TestClient,
) -> None:
    """T5/T6: updating or deleting someone else's record (or any record we didn't create) is refused."""
    foreign = v.issues.create_issue("Not created by Course Autopilot")

    response = http.post(
        "/proposals",
        json={"changes": [{"op": "delete_issue", "course": COURSE, "issue_id": foreign.id}]},
        headers=ALICE,
    )

    assert response.status_code == 400
    assert "didn't create" in response.json()["error"]["message"]


def test_another_users_proposal_is_not_found(v: Verticals, http: TestClient) -> None:
    """T2: bob can't read, approve, reject or execute alice's proposal (control: alice can read it)."""
    proposal = propose(http, ISSUE)
    pid = proposal["id"]

    assert http.get(f"/proposals/{pid}", headers=BOB).status_code == 404
    assert (
        http.post(
            f"/proposals/{pid}/approve",
            json={"changes_hash": proposal["changes_hash"]},
            headers=BOB,
        ).status_code
        == 404
    )
    assert http.post(f"/proposals/{pid}/reject", headers=BOB).status_code == 404
    assert http.post(f"/proposals/{pid}/execute", headers=BOB).status_code == 404
    assert http.get(f"/proposals/{pid}", headers=ALICE).json()["status"] == "pending"


def test_approval_needs_the_hash_of_the_current_content(v: Verticals, http: TestClient) -> None:
    """T3: approving with any other hash is 409 and leaves it pending (control: the right hash approves)."""
    proposal = propose(http, ISSUE)
    stale = changes_hash([{**ISSUE, "title": "something else"}])

    response = http.post(
        f"/proposals/{proposal['id']}/approve",
        json={"changes_hash": stale},
        headers=ALICE,
    )

    assert response.status_code == 409
    assert http.get(f"/proposals/{proposal['id']}", headers=ALICE).json()["status"] == "pending"
    approve(http, proposal)


def test_a_proposal_is_approved_once(v: Verticals, http: TestClient) -> None:
    """T4: a replayed approval is 409."""
    proposal = propose(http, ISSUE)
    approve(http, proposal)

    response = http.post(
        f"/proposals/{proposal['id']}/approve",
        json={"changes_hash": proposal["changes_hash"]},
        headers=ALICE,
    )

    assert response.status_code == 409


def test_a_rejected_proposal_cant_be_approved_or_run(v: Verticals, http: TestClient) -> None:
    """Rejection is final."""
    proposal = propose(http, ISSUE)
    assert (
        http.post(f"/proposals/{proposal['id']}/reject", headers=ALICE).json()["status"]
        == "rejected"
    )

    assert (
        http.post(
            f"/proposals/{proposal['id']}/approve",
            json={"changes_hash": proposal["changes_hash"]},
            headers=ALICE,
        ).status_code
        == 409
    )
    assert http.post(f"/proposals/{proposal['id']}/reject", headers=ALICE).status_code == 409
    assert http.post(f"/proposals/{proposal['id']}/execute", headers=ALICE).status_code == 409
    assert v.issues.list_issues(limit=10) == []


def test_execution_without_approval_is_refused(v: Verticals, http: TestClient) -> None:
    """T14: whoever calls, an unapproved proposal never runs (control: approved, it runs)."""
    proposal = propose(http, ISSUE)

    assert http.post(f"/proposals/{proposal['id']}/execute", headers=ALICE).status_code == 409
    assert v.issues.list_issues(limit=10) == []
    approve(http, proposal)
    assert execute(http, proposal)["status"] == "done"


def test_product_routes_need_a_signed_in_user(
    v: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No header, a malformed one, dev sign-in off, or the demo service: 401."""
    assert http.post("/proposals", json={"changes": [ISSUE]}).status_code == 401
    assert (
        http.post(
            "/proposals",
            json={"changes": [ISSUE]},
            headers={"X-Dev-User": "Not Valid!"},
        ).status_code
        == 401
    )
    monkeypatch.setenv("K_SERVICE", "cloud-storage-service-demo")
    assert http.post("/proposals", json={"changes": [ISSUE]}, headers=ALICE).status_code == 401
    monkeypatch.delenv(DEV_SIGN_IN_ENV_VAR)
    monkeypatch.delenv("K_SERVICE")
    assert http.post("/proposals", json={"changes": [ISSUE]}, headers=ALICE).status_code == 401


def test_without_a_database_product_routes_answer_503(
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service still starts without a database; proposals then say so."""
    monkeypatch.setenv(DEV_SIGN_IN_ENV_VAR, "enabled")
    app.dependency_overrides[get_database] = lambda: None
    try:
        response = http.post("/proposals", json={"changes": [ISSUE]}, headers=ALICE)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503


# ----- #90: executing -----


def test_an_approved_proposal_runs_every_change_and_verifies_it(
    v: Verticals,
    http: TestClient,
    database: Database,
) -> None:
    """Event, card and document are written, read back and recorded as ours."""
    proposal = propose(http, EVENT, ISSUE, DOC)
    approve(http, proposal)

    body = execute(http, proposal)

    assert body["status"] == "done"
    assert statuses(body) == ["completed", "completed", "completed"]
    [card] = v.issues.list_issues(limit=10)
    assert card.title == ISSUE["title"]
    assert v.storage.get_metadata(PLAN).size == len(DOC["content"].encode())
    with database.session() as session:
        kinds = sorted(r.kind for r in session.scalars(select(ExternalRecord)))
    assert kinds == ["document", "event", "issue"]


def test_a_finished_proposal_cant_run_again(v: Verticals, http: TestClient) -> None:
    """T4: replaying execute is 409 and writes nothing twice."""
    proposal = propose(http, ISSUE)
    approve(http, proposal)
    execute(http, proposal)

    assert http.post(f"/proposals/{proposal['id']}/execute", headers=ALICE).status_code == 409
    assert len(v.issues.list_issues(limit=10)) == 1


def test_a_partial_failure_keeps_completed_changes_and_a_retry_runs_only_the_rest(
    v: Verticals,
    http: TestClient,
) -> None:
    """P3: the card stays, the event fails, the document doesn't run; the retry finishes the job once."""
    proposal = propose(http, ISSUE, EVENT, DOC)
    approve(http, proposal)
    v.calendar = FailingCalendar()

    first = execute(http, proposal)

    assert first["status"] == "failed"
    assert statuses(first) == ["completed", "failed"]
    assert "unreachable" in first["outcomes"][1]["detail"]
    assert "controlled failure" not in first["outcomes"][1]["detail"]  # provider text never shown

    v.calendar = InMemoryCalendar()
    second = execute(http, proposal)

    assert second["status"] == "done"
    assert statuses(second) == ["completed", "completed", "completed"]
    assert len(v.issues.list_issues(limit=10)) == 1  # the card wasn't created twice


def test_an_uncertain_write_is_never_retried_automatically(v: Verticals, http: TestClient) -> None:
    """A lost reply is ``uncertain``; a retry skips it and leaves it for the person."""
    proposal = propose(http, EVENT)
    approve(http, proposal)
    v.calendar = FailingCalendar(CalendarErrorCode.PROVIDER_ERROR, retryable=False)

    first = execute(http, proposal)
    assert statuses(first) == ["uncertain"]
    assert "check your calendar" in first["outcomes"][0]["detail"]

    v.calendar = InMemoryCalendar()
    second = execute(http, proposal)

    assert statuses(second) == ["uncertain"]
    assert second["status"] == "failed"
    assert v.calendar._events == {}  # nothing was created on the working calendar either


class MayHaveAppliedIssues(InMemoryIssueTracker):
    """Fails ``create_issue`` the way a lost reply does: ``may_have_applied=True``."""

    def create_issue(self, title: str) -> Issue:
        """Fail after the request may have reached the provider."""
        raise ServiceError(IssueErrorCode.PROVIDER_ERROR, "timeout", may_have_applied=True)


def test_an_issue_write_that_may_have_applied_is_uncertain(v: Verticals, http: TestClient) -> None:
    """The issue contract's ``may_have_applied`` becomes ``uncertain``, not ``failed``."""
    proposal = propose(http, ISSUE)
    approve(http, proposal)
    v.issues = MayHaveAppliedIssues()

    assert statuses(execute(http, proposal)) == ["uncertain"]


def test_an_issue_tracker_failure_before_writing_is_failed(v: Verticals, http: TestClient) -> None:
    """``may_have_applied=False`` is a plain failure, safe to retry."""
    proposal = propose(http, ISSUE)
    approve(http, proposal)
    v.issues = FailingIssueTracker()

    body = execute(http, proposal)

    assert statuses(body) == ["failed"]
    assert "unreachable" in body["outcomes"][0]["detail"]


def created(body: dict[str, Any], kind: str) -> str:
    """Return the provider ID a completed create reported, from its outcome detail."""
    detail: str = next(
        o["detail"] for o in body["outcomes"] if o["detail"].startswith(f"Created {kind}")
    )
    return detail.rsplit(" ", 1)[1]


def test_our_own_records_can_be_updated_and_deleted(v: Verticals, http: TestClient) -> None:
    """A second proposal moves the event and renames the card; a third removes both."""
    first = propose(http, EVENT, ISSUE)
    approve(http, first)
    done = execute(http, first)
    event_id, card_id = created(done, "event"), created(done, "card")

    moved = propose(
        http,
        {
            "op": "update_event",
            "course": COURSE,
            "event_id": event_id,
            "title": "Midterm (moved)",
            "start": "2026-12-11T14:00:00-05:00",
            "end": "2026-12-11T15:30:00-05:00",
        },
        {
            "op": "update_issue",
            "course": COURSE,
            "issue_id": card_id,
            "title": "[CS-GY 6083] Midterm, Dec 11",
        },
    )
    approve(http, moved)
    assert execute(http, moved)["status"] == "done"
    assert v.calendar.get(event_id).title == "Midterm (moved)"

    removed = propose(
        http,
        {"op": "delete_event", "course": COURSE, "event_id": event_id},
        {"op": "delete_issue", "course": COURSE, "issue_id": card_id},
    )
    approve(http, removed)
    assert execute(http, removed)["status"] == "done"
    assert v.issues.list_issues(limit=10) == []

    # Deleted records are no longer "ours": a later proposal can't target them.
    again = http.post(
        "/proposals",
        json={"changes": [{"op": "delete_issue", "course": COURSE, "issue_id": card_id}]},
        headers=ALICE,
    )
    assert again.status_code == 400


def test_bob_cant_target_alices_records(v: Verticals, http: TestClient) -> None:
    """T6: record IDs are checked against the proposing user's own records."""
    done = propose(http, ISSUE)
    approve(http, done)
    card_id = created(execute(http, done), "card")

    response = http.post(
        "/proposals",
        json={"changes": [{"op": "delete_issue", "course": COURSE, "issue_id": card_id}]},
        headers=BOB,
    )

    assert response.status_code == 400
    assert len(v.issues.list_issues(limit=10)) == 1


def test_ownership_is_checked_again_when_the_proposal_runs(v: Verticals, http: TestClient) -> None:
    """A record removed after approval is reported, not touched (recheck at execution)."""
    done = propose(http, ISSUE)
    approve(http, done)
    card_id = created(execute(http, done), "card")
    delete = {"op": "delete_issue", "course": COURSE, "issue_id": card_id}
    first, second = propose(http, delete), propose(http, delete)
    approve(http, first)
    approve(http, second)
    execute(http, first)

    body = execute(http, second)

    assert statuses(body) == ["failed"]
    assert "already removed" in body["outcomes"][0]["detail"]


def test_a_document_we_wrote_is_replaced_and_a_foreign_one_is_left_alone(
    v: Verticals,
    http: TestClient,
) -> None:
    """Our course plan is updated in place; a file we didn't create is never overwritten."""
    first = propose(http, DOC)
    approve(http, first)
    execute(http, first)
    update = propose(http, {**DOC, "content": NEW_PLAN})
    approve(http, update)

    assert execute(http, update)["status"] == "done"
    assert v.storage.get_metadata(PLAN).size == len(NEW_PLAN.encode())

    v.storage.create(f"/Course Autopilot/{COURSE}/notes.md", b"the student's own notes")
    clash = propose(http, {**DOC, "name": "notes.md"})
    approve(http, clash)
    body = execute(http, clash)

    assert statuses(body) == ["failed"]
    assert "already exists" in body["outcomes"][0]["detail"]
    notes = f"/Course Autopilot/{COURSE}/notes.md"
    assert v.storage.get_metadata(notes).size == len(b"the student's own notes")
