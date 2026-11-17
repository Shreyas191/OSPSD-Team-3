"""The composed bug-report workflow: storage, then issue tracker, then calendar.

One operation, fixed steps, no language model:

1. ``storage``: save the report as ``/bug-reports/<report_id>.md`` (our Dropbox client).
2. ``issue_tracker``: create an issue titled ``[bug <short id>] <title>`` (``IssueProvider``).
3. ``calendar``: book a 30-minute triage event the next day at 15:00 UTC that names the
   issue (``CalendarService``).

Each vertical is reached only through its shared contract; which team's implementation
runs is chosen by configuration (see :mod:`verticals`).

**Partial failure: undo, then report.** If a step fails, the steps that already succeeded
are undone in reverse order (the issue is deleted, then the file), and the caller gets a
:class:`StepFailed` naming the failed step, what was undone, and anything that may be left
behind. So a failed run never claims success, and a retry starts from nothing: it gets a
new ``report_id`` and does not duplicate earlier work. Something can be left behind only
when an undo itself fails, or when the failed step was an *uncertain write* (the request
may have reached the provider although no answer came back). Every record carries the
``report_id``, so a stray one can be found by it.
"""

import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Literal, NoReturn

from calendar_contract import CalendarError, CalendarService, CreateEvent, Event
from calendar_contract import ErrorCode as CalendarErrorCode
from cloud_storage_client_api import Client, File
from issue_tracker_contract import Issue, IssueProvider, ServiceError

from cloud_storage_service.errors import classify_storage_error

__all__ = [
    "REPORT_FOLDER",
    "BugReport",
    "BugReportResult",
    "Step",
    "StepFailed",
    "run_bug_report",
]

Step = Literal["storage", "issue_tracker", "calendar"]

REPORT_FOLDER = "/bug-reports"
TRIAGE_TIME = time(15, 0, tzinfo=UTC)
TRIAGE_LENGTH = timedelta(minutes=30)
MAX_ISSUE_TITLE = 256  # issue_tracker_contract.MAX_TITLE_LENGTH

_BAD_GATEWAY, _UNAVAILABLE = 502, 503


@dataclass(frozen=True)
class BugReport:
    """What the caller reports. Fields are already validated by the HTTP layer."""

    title: str
    description: str
    reporter: str


@dataclass(frozen=True)
class BugReportResult:
    """The three records one successful run created."""

    report_id: str
    file: File
    issue: Issue
    event: Event


@dataclass
class StepFailed(Exception):  # noqa: N818 (named for what happened, like FileNotFoundError)
    """A step failed; earlier steps were undone as far as possible.

    ``status``, ``code`` and ``message`` follow the service's error model; ``message`` never
    contains provider details. ``undone`` lists the records removed again; ``left_behind``
    describes any that may remain (see the module docstring).
    """

    step: Step
    status: int
    code: str
    message: str
    report_id: str
    reason: str = ""  # internal only: logged, never returned to the caller
    undone: list[str] = field(default_factory=list)
    left_behind: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        """Describe the failure for logs (no user data)."""
        return f"{self.step} step failed: {self.code} ({self.reason})"


def run_bug_report(
    report: BugReport,
    *,
    storage: Client,
    issues: IssueProvider,
    calendar: CalendarService,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BugReportResult:
    """Run the three steps in order and return the records they created.

    Raises:
        StepFailed: If a step fails, after undoing the steps that succeeded.

    """
    report_id = uuid.uuid4().hex
    path = f"{REPORT_FOLDER}/{report_id}.md"
    undo: list[tuple[str, Callable[[], object]]] = []  # (description, action), in run order

    def fail(failure: StepFailed, *, uncertain: str) -> NoReturn:
        """Undo the finished steps, newest first, then raise ``failure``."""
        if uncertain:
            failure.left_behind.append(uncertain)
        for description, action in reversed(undo):
            try:
                action()
                failure.undone.append(description)
            except (ServiceError, CalendarError, ValueError, OSError):
                failure.left_behind.append(description)
        raise failure

    # 1. Storage: the report itself.
    try:
        file = _save_report(storage, path, report_id, report, now())
    except (ValueError, OSError) as error:
        status, code, message = classify_storage_error(error)
        # A provider failure (5xx) may hide a saved file behind a lost reply. Its path is
        # known, so remove it. Never on a client error: a conflict means the file is not ours.
        uncertain = ""
        if status >= _BAD_GATEWAY and not _remove_quietly(storage, path):
            uncertain = f"possibly file {path}"
        failure = StepFailed("storage", status, code, message, report_id, str(error))
        fail(failure, uncertain=uncertain)
    undo.append((f"file {path}", lambda: storage.delete(path)))

    # 2. Issue tracker: an issue that points back to the report.
    title = f"[bug {report_id[:8]}] {report.title}"[:MAX_ISSUE_TITLE]
    try:
        issue = issues.create_issue(title)
    except ServiceError as error:
        uncertain = (
            f"possibly an issue titled '[bug {report_id[:8]}] …'" if error.may_have_applied else ""
        )
        failure = StepFailed(
            "issue_tracker",
            _BAD_GATEWAY,
            "provider_error",
            "The issue tracker failed to create the issue",
            report_id,
            f"{error.code}: {error.cause or error.message}",
        )
        fail(failure, uncertain=uncertain)
    undo.append((f"issue {issue.id}", lambda: issues.delete_issue(issue.id)))

    # 3. Calendar: a triage meeting about the issue.
    start = datetime.combine(now().date() + timedelta(days=1), TRIAGE_TIME)
    try:
        event = calendar.create(
            CreateEvent(
                title=f"Triage: {report.title} (issue {issue.id})",
                start=start.isoformat(),
                end=(start + TRIAGE_LENGTH).isoformat(),
            ),
        )
    except CalendarError as error:
        unavailable = error.code == CalendarErrorCode.PROVIDER_UNAVAILABLE
        # The contract reports an uncertain write as non-retryable provider_error.
        maybe_written = error.code == CalendarErrorCode.PROVIDER_ERROR and not error.retryable
        failure = StepFailed(
            "calendar",
            _UNAVAILABLE if unavailable else _BAD_GATEWAY,
            "provider_unavailable" if unavailable else "provider_error",
            "The calendar is unreachable; try again later"
            if unavailable
            else "The calendar failed to create the event",
            report_id,
            f"{error.code}: {error.message}",
        )
        uncertain = f"possibly an event titled 'Triage: … (issue {issue.id})'"
        fail(failure, uncertain=uncertain if maybe_written else "")

    return BugReportResult(report_id=report_id, file=file, issue=issue, event=event)


def _save_report(
    storage: Client,
    path: str,
    report_id: str,
    report: BugReport,
    created: datetime,
) -> File:
    body = (
        f"# {report.title}\n\n"
        f"- Report: {report_id}\n"
        f"- Reporter: {report.reporter}\n"
        f"- Created: {created.astimezone(UTC).isoformat()}\n\n"
        f"{report.description}\n"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        local = Path(tmp_dir) / "report.md"
        local.write_text(body, encoding="utf-8")
        return storage.upload_file(str(local), path, overwrite=False)


def _remove_quietly(storage: Client, path: str) -> bool:
    """Delete ``path`` if it exists; True when nothing is left there (or never was)."""
    try:
        storage.delete(path)
    except FileNotFoundError:
        return True
    except (ValueError, OSError):
        return False
    return True
