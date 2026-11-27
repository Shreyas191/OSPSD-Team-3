"""The executor's less common outcomes (#90): read-backs that don't match, and provider errors.

``test_proposal_routes.py`` covers the main paths over HTTP. Here each change runs directly
through ``execution.execute`` against stub providers that misbehave in one specific way.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from calendar_contract import CalendarError, CreateEvent, Event, ReplaceEvent
from calendar_contract import ErrorCode as CalendarErrorCode
from cloud_storage_api.errors import ProviderError, ProviderUnavailableError
from course_autopilot_store import Database
from course_autopilot_store.models import ChangeOutcome, ExternalRecord, User
from dropbox_client_impl import DropboxDocumentStorage
from issue_tracker_contract.errors import ErrorCode as IssueErrorCode
from issue_tracker_contract.errors import ServiceError
from issue_tracker_contract.models import Issue, IssueUpdate
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from tests.fakes import FakeStorageClient

from cloud_storage_service import execution, proposals
from cloud_storage_service.fake_verticals import InMemoryCalendar, InMemoryIssueTracker

COURSE = "CS-GY 6083"
START, END = "2026-12-10T14:00:00-05:00", "2026-12-10T15:30:00-05:00"


@pytest.fixture
def database() -> Iterator[Database]:
    """Provide a migrated in-memory database."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    db = Database(engine)
    db.upgrade()
    yield db
    engine.dispose()


def run(
    db: Database,
    change: dict[str, Any],
    v: execution.Verticals,
    *,
    owned: tuple[str, str] | None = None,
) -> ChangeOutcome:
    """Store an approved one-change proposal for a fresh user, execute it, return its outcome."""
    with db.session() as session:
        user = User(auth_provider="dev", auth_subject=uuid.uuid4().hex[:12])
        session.add(user)
        session.flush()
        user_id = user.id
        if owned:
            session.add(
                ExternalRecord(
                    user_id=user_id,
                    course_key=COURSE,
                    kind=owned[0],
                    provider_record_id=owned[1],
                ),
            )
        body = proposals.ProposalCreate.model_validate({"changes": [change]})
        proposal = proposals.create(session, user_id, body)
        proposals.approve(session, user_id, proposal.id, proposal.changes_hash)
        proposal_id = proposal.id
    execution.execute(db, user_id, proposal_id, v)
    with db.session() as session:
        outcome = session.scalars(
            select(ChangeOutcome).where(ChangeOutcome.proposal_id == proposal_id),
        ).one()
        session.expunge(outcome)
        return outcome


def verticals(
    *,
    calendar: Any = None,
    issues: Any = None,
    storage: Any = None,
) -> execution.Verticals:
    """Working in-memory providers, with any of them replaced."""
    return execution.Verticals(
        storage=storage or DropboxDocumentStorage(FakeStorageClient()),
        issues=issues or InMemoryIssueTracker(),
        calendar=calendar or InMemoryCalendar(),
    )


# ----- Read-backs that don't match the write: uncertain -----


class CalendarReadsBackOtherTitle(InMemoryCalendar):
    """Stores events, but ``get`` reports a different title."""

    def get(self, event_id: str) -> Event:
        """Return the event with its title changed."""
        return super().get(event_id).model_copy(update={"title": "something else"})


def test_an_event_that_reads_back_differently_is_uncertain(database: Database) -> None:
    """Created, but not as asked: the person must check."""
    change = {
        "op": "create_event",
        "course": COURSE,
        "title": "Midterm",
        "start": START,
        "end": END,
    }

    outcome = run(database, change, verticals(calendar=CalendarReadsBackOtherTitle()))

    assert outcome.status == "uncertain"
    assert "reads back differently" in outcome.detail


def test_an_updated_event_that_reads_back_differently_is_uncertain(database: Database) -> None:
    """The same check after an update."""
    calendar = CalendarReadsBackOtherTitle()
    event = InMemoryCalendar.create(calendar, CreateEvent(title="Midterm", start=START, end=END))
    change = {
        "op": "update_event",
        "course": COURSE,
        "event_id": event.id,
        "title": "Midterm",
        "start": START,
        "end": END,
    }

    outcome = run(database, change, verticals(calendar=calendar), owned=("event", event.id))

    assert outcome.status == "uncertain"


class CalendarThatKeepsDeletedEvents(InMemoryCalendar):
    """``delete`` succeeds but the event is still there."""

    def delete(self, event_id: str) -> None:
        """Pretend to delete."""


def test_an_event_still_present_after_delete_is_uncertain(database: Database) -> None:
    """A delete is only completed when the event is really gone."""
    calendar = CalendarThatKeepsDeletedEvents()
    event = calendar.create(CreateEvent(title="Midterm", start=START, end=END))

    outcome = run(
        database,
        {"op": "delete_event", "course": COURSE, "event_id": event.id},
        verticals(calendar=calendar),
        owned=("event", event.id),
    )

    assert outcome.status == "uncertain"
    assert "still exists" in outcome.detail


class IssuesReadBackOtherTitle(InMemoryIssueTracker):
    """Stores cards, but ``get_issue`` reports a different title."""

    def get_issue(self, issue_id: str, *, fresh: bool = False) -> Issue:
        """Return the card with its title changed."""
        return (
            super().get_issue(issue_id, fresh=fresh).model_copy(update={"title": "something else"})
        )


def test_a_card_that_reads_back_differently_is_uncertain(database: Database) -> None:
    """Created or renamed, but not as asked."""
    issues = IssuesReadBackOtherTitle()
    assert (
        run(
            database,
            {"op": "create_issue", "course": COURSE, "title": "HW1"},
            verticals(issues=issues),
        ).status
        == "uncertain"
    )

    card = issues.create_issue("HW1")
    outcome = run(
        database,
        {"op": "update_issue", "course": COURSE, "issue_id": card.id, "title": "HW1 v2"},
        verticals(issues=issues),
        owned=("issue", card.id),
    )
    assert outcome.status == "uncertain"


class IssuesThatKeepDeletedCards(InMemoryIssueTracker):
    """``delete_issue`` succeeds but the card is still there."""

    def delete_issue(self, issue_id: str) -> None:
        """Pretend to delete."""


def test_a_card_still_present_after_delete_is_uncertain(database: Database) -> None:
    """A delete is only completed when the card is really gone."""
    issues = IssuesThatKeepDeletedCards()
    card = issues.create_issue("HW1")

    outcome = run(
        database,
        {"op": "delete_issue", "course": COURSE, "issue_id": card.id},
        verticals(issues=issues),
        owned=("issue", card.id),
    )

    assert outcome.status == "uncertain"


# ----- Provider errors -----


class CalendarFailingOn(InMemoryCalendar):
    """Raise one ``CalendarError`` from ``replace`` and ``delete``."""

    def __init__(self, code: CalendarErrorCode) -> None:
        """Fail with ``code``."""
        super().__init__()
        self.code = code

    def replace(self, event_id: str, request: ReplaceEvent) -> Event:
        """Fail."""
        raise CalendarError(self.code, "provider says no", retryable=False)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (CalendarErrorCode.NOT_FOUND, "no longer exists"),
        (CalendarErrorCode.ACCESS_DENIED, "reconnect Google Calendar"),
    ],
)
def test_calendar_errors_become_fixed_messages(
    database: Database,
    code: CalendarErrorCode,
    expected: str,
) -> None:
    """The person sees what to do, never the provider's own text."""
    calendar = CalendarFailingOn(code)
    event = calendar.create(CreateEvent(title="Midterm", start=START, end=END))
    change = {
        "op": "update_event",
        "course": COURSE,
        "event_id": event.id,
        "title": "Midterm 2",
        "start": START,
        "end": END,
    }

    outcome = run(database, change, verticals(calendar=calendar), owned=("event", event.id))

    assert (outcome.status, expected in outcome.detail, "provider says no" in outcome.detail) == (
        "failed",
        True,
        False,
    )


class CalendarDeleteThenAccessDenied(InMemoryCalendar):
    """The delete works, but the read-back is refused."""

    def get(self, event_id: str) -> Event:
        """Refuse."""
        raise CalendarError(CalendarErrorCode.ACCESS_DENIED, "no", retryable=False)


def test_a_refused_read_back_after_delete_is_reported_as_the_error(database: Database) -> None:
    """Only ``not_found`` proves a delete; any other error is reported as itself."""
    calendar = CalendarDeleteThenAccessDenied()
    event = InMemoryCalendar.create(calendar, CreateEvent(title="Midterm", start=START, end=END))

    outcome = run(
        database,
        {"op": "delete_event", "course": COURSE, "event_id": event.id},
        verticals(calendar=calendar),
        owned=("event", event.id),
    )

    assert outcome.status == "failed"
    assert "reconnect" in outcome.detail


class IssuesFailingOn(InMemoryIssueTracker):
    """Raise one ``ServiceError`` from ``update_issue``, and refuse read-backs after a delete."""

    def __init__(self, code: IssueErrorCode) -> None:
        """Fail with ``code``."""
        super().__init__()
        self.code = code

    def update_issue(self, issue_id: str, changes: IssueUpdate) -> Issue:
        """Fail."""
        raise ServiceError(self.code, "provider says no", may_have_applied=False)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (IssueErrorCode.NOT_FOUND, "no longer exists"),
        (IssueErrorCode.ACCESS_DENIED, "reconnect Trello"),
    ],
)
def test_issue_errors_become_fixed_messages(
    database: Database,
    code: IssueErrorCode,
    expected: str,
) -> None:
    """The person sees what to do, never the provider's own text."""
    issues = IssuesFailingOn(code)
    card = issues.create_issue("HW1")

    outcome = run(
        database,
        {"op": "update_issue", "course": COURSE, "issue_id": card.id, "title": "HW1 v2"},
        verticals(issues=issues),
        owned=("issue", card.id),
    )

    assert (outcome.status, expected in outcome.detail) == ("failed", True)


class IssuesDeleteThenRefused(InMemoryIssueTracker):
    """The delete works, but the read-back is refused."""

    def get_issue(self, issue_id: str, *, fresh: bool = False) -> Issue:
        """Refuse."""
        raise ServiceError(IssueErrorCode.ACCESS_DENIED, "no", may_have_applied=False)


def test_a_refused_card_read_back_after_delete_is_reported_as_the_error(database: Database) -> None:
    """Only ``not_found`` proves a delete."""
    issues = IssuesDeleteThenRefused()
    card = InMemoryIssueTracker.create_issue(issues, "HW1")

    outcome = run(
        database,
        {"op": "delete_issue", "course": COURSE, "issue_id": card.id},
        verticals(issues=issues),
        owned=("issue", card.id),
    )

    assert outcome.status == "failed"


def test_records_this_user_didnt_create_are_left_alone_at_execution(database: Database) -> None:
    """The ownership recheck at execution covers every update and delete operation."""
    issues, calendar = InMemoryIssueTracker(), InMemoryCalendar()
    card = issues.create_issue("HW1")
    event = calendar.create(CreateEvent(title="Midterm", start=START, end=END))
    v = verticals(issues=issues, calendar=calendar)
    changes = [
        (
            {
                "op": "update_event",
                "course": COURSE,
                "event_id": event.id,
                "title": "x",
                "start": START,
                "end": END,
            },
            ("event", event.id),
        ),
        ({"op": "delete_event", "course": COURSE, "event_id": event.id}, ("event", event.id)),
        (
            {"op": "update_issue", "course": COURSE, "issue_id": card.id, "title": "x"},
            ("issue", card.id),
        ),
    ]
    for change, record in changes:
        # Owned when proposed, then removed from the user's records before execution.
        with database.session() as session:
            session.query(ExternalRecord).delete()
        outcome = run_after_unowning(database, change, v, record)
        assert outcome.status == "failed"
        assert "already removed" in outcome.detail


def run_after_unowning(
    db: Database,
    change: dict[str, Any],
    v: execution.Verticals,
    record: tuple[str, str],
) -> ChangeOutcome:
    """Like ``run``, but the record stops being ours between approval and execution."""
    with db.session() as session:
        user = User(auth_provider="dev", auth_subject=uuid.uuid4().hex[:12])
        session.add(user)
        session.flush()
        user_id = user.id
        session.add(
            ExternalRecord(
                user_id=user_id,
                course_key=COURSE,
                kind=record[0],
                provider_record_id=record[1],
            ),
        )
        session.flush()
        proposal = proposals.create(
            session,
            user_id,
            proposals.ProposalCreate.model_validate({"changes": [change]}),
        )
        proposals.approve(session, user_id, proposal.id, proposal.changes_hash)
        proposal_id = proposal.id
    with db.session() as session:
        session.query(ExternalRecord).filter(ExternalRecord.user_id == user_id).delete()
    execution.execute(db, user_id, proposal_id, v)
    with db.session() as session:
        outcome = session.scalars(
            select(ChangeOutcome).where(ChangeOutcome.proposal_id == proposal_id),
        ).one()
        session.expunge(outcome)
        return outcome


class StorageRaising(DropboxDocumentStorage):
    """``create`` raises one storage error."""

    def __init__(self, error: Exception) -> None:
        """Fail with ``error``."""
        super().__init__(FakeStorageClient())
        self.error = error

    def create(self, path: str, content: bytes) -> Any:
        """Fail."""
        raise self.error


class StorageWithWrongSize(DropboxDocumentStorage):
    """Stores the file, but its metadata reports another size."""

    def get_metadata(self, path: str) -> Any:
        """Return the metadata with a size that doesn't match."""
        info = super().get_metadata(path)
        return type(info)(**{**info.__dict__, "size": (info.size or 0) + 1})


DOC = {"op": "write_document", "course": COURSE, "name": "plan.md", "content": "# Plan\n"}


def test_a_lost_upload_reply_is_uncertain(database: Database) -> None:
    """A storage provider error may follow a write that happened."""
    outcome = run(database, DOC, verticals(storage=StorageRaising(ProviderError("lost"))))

    assert outcome.status == "uncertain"


def test_an_unavailable_storage_is_failed(database: Database) -> None:
    """Unavailable before writing: safe to retry."""
    outcome = run(
        database,
        DOC,
        verticals(storage=StorageRaising(ProviderUnavailableError("down"))),
    )

    assert (outcome.status, "unreachable" in outcome.detail) == ("failed", True)


def test_a_document_with_the_wrong_size_is_uncertain(database: Database) -> None:
    """The metadata read-back must match what we wrote."""
    outcome = run(database, DOC, verticals(storage=StorageWithWrongSize(FakeStorageClient())))

    assert outcome.status == "uncertain"
