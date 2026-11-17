"""HTTP tests for ``POST /workflows/bug-report``, the composed workflow.

All three verticals are replaced through ``app.dependency_overrides`` with controlled
implementations (the in-memory storage fake and the issue-tracker and calendar fakes), so
these tests need no credentials and no network. Storage is the real shared-contract
wrapper (``DropboxDocumentStorage``) over the in-memory ``Client`` fake, the same layering
the app builds at startup, so storage failures injected into the fake reach the workflow
as the shared ``cloud_storage_api`` errors. They check the combined result, and for a
failure at each step: the status, the error body naming the step, what was undone, and
what each vertical holds afterwards.
"""

import importlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from calendar_contract import CalendarError, CreateEvent, Event
from calendar_contract import ErrorCode as CalendarErrorCode
from cloud_storage_api import DocumentStorage
from dropbox_client_impl import DropboxDocumentStorage
from fastapi.testclient import TestClient
from issue_tracker_contract import ErrorCode as IssueErrorCode
from issue_tracker_contract import Issue, ServiceError
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app
from cloud_storage_service.dependencies import (
    get_calendar,
    get_document_storage,
    get_issue_tracker,
)
from cloud_storage_service.fake_verticals import (
    FailingCalendar,
    FailingIssueTracker,
    InMemoryCalendar,
    InMemoryIssueTracker,
)
from cloud_storage_service.workflow import REPORT_FOLDER

URL = "/workflows/bug-report"
REPORT = {
    "title": "Login fails after password reset",
    "description": "Reset the password, log in with the new one: 500.",
    "reporter": "victor",
}


class Verticals:
    """The three controlled implementations a test runs against; replaceable per test."""

    def __init__(self) -> None:
        """Start with working, empty implementations."""
        self.storage = FakeStorageClient()
        self.issues: InMemoryIssueTracker | FailingIssueTracker = InMemoryIssueTracker()
        self.calendar: InMemoryCalendar | FailingCalendar = InMemoryCalendar()


@pytest.fixture
def verticals() -> Iterator[Verticals]:
    """Point the app's three dependencies at controlled implementations."""
    v = Verticals()
    app.dependency_overrides[get_document_storage] = lambda: DropboxDocumentStorage(v.storage)
    app.dependency_overrides[get_issue_tracker] = lambda: v.issues
    app.dependency_overrides[get_calendar] = lambda: v.calendar
    yield v
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Create a client without starting the lifespan (which would build real clients)."""
    return TestClient(app)


def reports(v: Verticals) -> list[str]:
    """Paths of the bug reports in storage."""
    return [p for p in v.storage.entries if p.startswith(f"{REPORT_FOLDER}/")]


def issues(v: Verticals) -> list[Issue]:
    """Issues in the in-memory tracker."""
    assert isinstance(v.issues, InMemoryIssueTracker)
    return v.issues.list_issues(limit=100)


# --- Success -------------------------------------------------------------------------------


def test_creates_one_record_in_each_vertical_and_links_them(
    verticals: Verticals,
    http: TestClient,
) -> None:
    """201 with the file, the issue and the event, each pointing to the one before."""
    response = http.post(URL, json=REPORT)

    assert response.status_code == 201
    body = response.json()
    report_id = body["report_id"]
    assert body["file"]["path"] == f"{REPORT_FOLDER}/{report_id}.md"
    assert body["issue"]["title"] == f"[bug {report_id[:8]}] {REPORT['title']}"
    assert body["event"]["title"] == f"Triage: {REPORT['title']} (issue {body['issue']['id']})"
    # What each provider holds is what the response says.
    assert reports(verticals) == [body["file"]["path"]]
    assert [i.id for i in issues(verticals)] == [body["issue"]["id"]]
    assert isinstance(verticals.calendar, InMemoryCalendar)
    assert verticals.calendar.get(body["event"]["id"]).title == body["event"]["title"]


def test_triage_is_thirty_minutes_tomorrow_at_15_utc(
    verticals: Verticals,
    http: TestClient,
) -> None:
    """The event starts the next day at 15:00 UTC and lasts 30 minutes."""
    event = http.post(URL, json=REPORT).json()["event"]

    start, end = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    assert start == datetime(tomorrow.year, tomorrow.month, tomorrow.day, 15, tzinfo=UTC)
    assert end - start == timedelta(minutes=30)


def test_report_file_holds_the_report(verticals: Verticals, http: TestClient) -> None:
    """The saved file is the report as Markdown (its size matches what was written)."""
    body = http.post(URL, json=REPORT).json()

    expected_min = len(REPORT["title"]) + len(REPORT["description"]) + len(REPORT["reporter"])
    assert body["file"]["size"] > expected_min
    assert verticals.storage.entries[body["file"]["path"]].size == body["file"]["size"]


# --- A failure at each step: error names the step, earlier steps are undone ----------------


def test_storage_unavailable_fails_the_storage_step_and_creates_nothing(
    verticals: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 provider_unavailable, step storage; the issue tracker and calendar are untouched."""

    def unreachable(*_args: object, **_kwargs: object) -> None:
        msg = "Dropbox is unreachable"
        raise ConnectionError(msg)

    monkeypatch.setattr(verticals.storage, "upload_file", unreachable)

    response = http.post(URL, json=REPORT)

    assert response.status_code == 503
    error = response.json()["error"]
    assert (error["code"], error["step"]) == ("provider_unavailable", "storage")
    assert error["undone"] == []
    assert error["left_behind"] == []
    assert "Dropbox" not in error["message"]  # provider details stay in the log
    assert issues(verticals) == []


def test_storage_conflict_is_reported_and_the_existing_file_is_kept(
    verticals: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client error (409 conflict) never deletes the file that was already there."""
    existing = verticals.storage.add(f"{REPORT_FOLDER}/taken.md", size=3)

    def conflict(*_args: object, **_kwargs: object) -> None:
        msg = "Something already exists"
        raise FileExistsError(msg)

    monkeypatch.setattr(verticals.storage, "upload_file", conflict)

    response = http.post(URL, json=REPORT)

    assert response.status_code == 409
    assert response.json()["error"]["step"] == "storage"
    assert verticals.storage.entries[existing.path] is existing


def test_issue_tracker_failure_undoes_the_file(verticals: Verticals, http: TestClient) -> None:
    """502 provider_error, step issue_tracker; the saved report is deleted again."""
    verticals.issues = FailingIssueTracker()

    response = http.post(URL, json=REPORT)

    assert response.status_code == 502
    error = response.json()["error"]
    assert (error["code"], error["step"]) == ("provider_error", "issue_tracker")
    assert error["undone"] == [f"file {REPORT_FOLDER}/{error['report_id']}.md"]
    assert error["left_behind"] == []
    assert reports(verticals) == []


def test_calendar_unavailable_undoes_the_issue_then_the_file(
    verticals: Verticals,
    http: TestClient,
) -> None:
    """503 provider_unavailable, step calendar; the issue and the file are removed, newest first."""
    verticals.calendar = FailingCalendar()

    response = http.post(URL, json=REPORT)

    assert response.status_code == 503
    error = response.json()["error"]
    assert (error["code"], error["step"]) == ("provider_unavailable", "calendar")
    assert error["undone"][0].startswith("issue ")
    assert error["undone"][1] == f"file {REPORT_FOLDER}/{error['report_id']}.md"
    assert error["left_behind"] == []
    assert reports(verticals) == []
    assert issues(verticals) == []


def test_calendar_other_errors_are_provider_error(verticals: Verticals, http: TestClient) -> None:
    """A calendar failure other than unavailable is 502 provider_error."""
    verticals.calendar = FailingCalendar(CalendarErrorCode.ACCESS_DENIED, retryable=False)

    response = http.post(URL, json=REPORT)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert response.json()["error"]["left_behind"] == []


# --- Uncertain writes and failed undos are reported, never hidden --------------------------


def test_uncertain_calendar_write_is_reported_as_possibly_left_behind(
    verticals: Verticals,
    http: TestClient,
) -> None:
    """The contract's uncertain write (provider_error, not retryable) may have made an event."""
    verticals.calendar = FailingCalendar(CalendarErrorCode.PROVIDER_ERROR, retryable=False)

    error = http.post(URL, json=REPORT).json()["error"]

    assert error["step"] == "calendar"
    assert len(error["left_behind"]) == 1
    assert error["left_behind"][0].startswith("possibly an event")


def test_uncertain_issue_write_is_reported_as_possibly_left_behind(
    verticals: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An issue-tracker failure that ``may_have_applied`` may have left an issue."""

    def lost_reply(_title: str) -> Issue:
        raise ServiceError(IssueErrorCode.PROVIDER_ERROR, "No reply", may_have_applied=True)

    monkeypatch.setattr(verticals.issues, "create_issue", lost_reply)

    error = http.post(URL, json=REPORT).json()["error"]

    assert error["step"] == "issue_tracker"
    assert error["left_behind"] == [f"possibly an issue titled '[bug {error['report_id'][:8]}] …'"]
    assert reports(verticals) == []  # the file was still undone


def test_storage_provider_failure_removes_a_file_the_lost_reply_hid(
    verticals: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upload reached storage but the reply was lost: the file is found and removed."""
    original = verticals.storage.upload_file

    def saved_then_lost(local_path: str, remote_path: str, *, overwrite: bool = False) -> None:
        original(local_path, remote_path, overwrite=overwrite)
        msg = "reply lost"
        raise ConnectionError(msg)

    monkeypatch.setattr(verticals.storage, "upload_file", saved_then_lost)

    error = http.post(URL, json=REPORT).json()["error"]

    assert error["step"] == "storage"
    assert error["left_behind"] == []
    assert reports(verticals) == []


def test_storage_cleanup_that_fails_is_reported(
    verticals: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the hidden file cannot be checked or removed, the error says it may remain."""

    def unreachable(*_args: object, **_kwargs: object) -> None:
        msg = "unreachable"
        raise ConnectionError(msg)

    monkeypatch.setattr(verticals.storage, "upload_file", unreachable)
    monkeypatch.setattr(verticals.storage, "delete", unreachable)

    error = http.post(URL, json=REPORT).json()["error"]

    assert error["left_behind"] == [f"possibly file {REPORT_FOLDER}/{error['report_id']}.md"]


def test_an_undo_that_fails_is_listed_as_left_behind(
    verticals: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The calendar fails and deleting the issue fails too: the issue is reported, the file undone."""
    verticals.calendar = FailingCalendar()

    def cannot_delete(_issue_id: str) -> None:
        raise ServiceError(IssueErrorCode.PROVIDER_ERROR, "down")

    monkeypatch.setattr(verticals.issues, "delete_issue", cannot_delete)

    error = http.post(URL, json=REPORT).json()["error"]

    assert len(error["left_behind"]) == 1
    assert error["left_behind"][0].startswith("issue ")
    assert error["undone"] == [f"file {REPORT_FOLDER}/{error['report_id']}.md"]
    assert len(issues(verticals)) == 1


# --- Retrying after a failure does not duplicate work --------------------------------------


def test_retry_after_a_calendar_failure_leaves_exactly_one_of_each(
    verticals: Verticals,
    http: TestClient,
) -> None:
    """Fail at the calendar, then retry once it works: one file, one issue, one event in total."""
    verticals.calendar = FailingCalendar()
    first = http.post(URL, json=REPORT)
    assert first.status_code == 503

    verticals.calendar = InMemoryCalendar()
    second = http.post(URL, json=REPORT)

    assert second.status_code == 201
    assert second.json()["report_id"] != first.json()["error"]["report_id"]
    assert len(reports(verticals)) == 1
    assert len(issues(verticals)) == 1


# --- Input validation (before any provider is called) --------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {**REPORT, "title": "   "},
        {**REPORT, "title": "x" * 121},
        {k: v for k, v in REPORT.items() if k != "reporter"},
        {**REPORT, "priority": "high"},
    ],
    ids=["blank title", "title too long", "missing reporter", "unknown field"],
)
def test_invalid_input_is_400_and_calls_no_provider(
    verticals: Verticals,
    http: TestClient,
    body: dict[str, str],
) -> None:
    """400 invalid_request in the shared error body; nothing is created anywhere."""
    response = http.post(URL, json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert reports(verticals) == []
    assert issues(verticals) == []


# --- The calendar request the workflow sends follows the calendar contract -----------------


def test_calendar_receives_a_valid_contract_request(
    verticals: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow builds a ``CreateEvent`` that passes the contract's own validation."""
    seen: list[CreateEvent] = []
    calendar = verticals.calendar
    assert isinstance(calendar, InMemoryCalendar)
    original = calendar.create

    def record(request: CreateEvent) -> Event:
        seen.append(request)
        return original(request)

    monkeypatch.setattr(calendar, "create", record)

    http.post(URL, json=REPORT)

    assert len(seen) == 1
    CreateEvent.model_validate(seen[0].model_dump())  # raises if invalid


def test_calendar_errors_are_not_retried(verticals: Verticals, http: TestClient) -> None:
    """One failed create means one call: the workflow never retries a write by itself."""
    calls: list[str] = []

    class CountingFailingCalendar(FailingCalendar):
        def create(self, request: CreateEvent) -> Event:
            calls.append(request.title)
            return super().create(request)

    verticals.calendar = CountingFailingCalendar()
    http.post(URL, json=REPORT)

    assert len(calls) == 1


def test_issue_ids_reach_the_calendar_title(verticals: Verticals, http: TestClient) -> None:
    """The event names the issue, so the triage meeting can be traced to it."""
    body = http.post(URL, json=REPORT).json()

    assert body["issue"]["id"] in body["event"]["title"]


def test_calendar_error_message_hides_provider_details(
    verticals: Verticals,
    http: TestClient,
) -> None:
    """The caller sees a fixed message; the calendar's own message only goes to the log."""

    class LeakyCalendar(FailingCalendar):
        def create(self, request: CreateEvent) -> Event:
            raise CalendarError(
                CalendarErrorCode.PROVIDER_ERROR,
                "googleapis 500 secret",
                retryable=True,
            )

    verticals.calendar = LeakyCalendar()

    error = http.post(URL, json=REPORT).json()["error"]

    assert "googleapis" not in error["message"]


# --- Storage goes through the shared cloud_storage_api contract -----------------------------


@pytest.mark.parametrize(
    ("client_error", "status", "code", "keeps_message"),
    [
        (PermissionError("No access to /bug-reports"), 403, "permission_denied", True),
        (ValueError("Not a valid upload"), 422, "invalid_operation", True),
        (OSError("dropbox internal detail"), 502, "provider_error", False),
        (ConnectionError("dropbox internal detail"), 503, "provider_unavailable", False),
    ],
)
def test_storage_errors_follow_the_shared_contract_and_hide_provider_details(  # noqa: PLR0913
    verticals: Verticals,
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    client_error: Exception,
    status: int,
    code: str,
    keeps_message: bool,  # noqa: FBT001
) -> None:
    """Each shared error keeps its status and code; 5xx messages are fixed, as on ``/files``."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise client_error

    monkeypatch.setattr(verticals.storage, "upload_file", fail)

    response = http.post(URL, json=REPORT)

    assert response.status_code == status
    error = response.json()["error"]
    assert (error["code"], error["step"]) == (code, "storage")
    assert (str(client_error) in error["message"]) is keeps_message
    assert issues(verticals) == []


def test_the_app_wraps_its_storage_client_in_the_shared_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At startup the workflow's storage is a ``DocumentStorage`` over the app's own client."""
    fake = FakeStorageClient()
    app_module = importlib.import_module("cloud_storage_service.app")
    monkeypatch.setattr(app_module, "build_storage_client", lambda: fake)
    for name in ("FAULT_INJECTION", "ISSUE_TRACKER_IMPL", "CALENDAR_IMPL", "K_SERVICE"):
        monkeypatch.delenv(name, raising=False)

    with TestClient(app) as client:  # runs the lifespan
        storage = app.state.document_storage
        response = client.post(URL, json=REPORT)

    assert isinstance(storage, DocumentStorage)
    assert response.status_code == 201
    assert response.json()["file"]["path"] in fake.entries
