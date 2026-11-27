"""Controlled implementations of the issue-tracker, calendar and document-storage contracts.

They let the service run, and the workflow be tested, without Trello or Google Calendar:

- :class:`InMemoryIssueTracker` and :class:`InMemoryCalendar` keep what they create in a
  dictionary for as long as the process runs. Unit tests and local development use them.
  :class:`InMemoryDocumentStorage` does the same for files, keeping their exact bytes.
- :class:`FailingIssueTracker` and :class:`FailingCalendar` fail every call with the
  contract's own error, the way an unavailable provider would. They demonstrate that the
  caller of a composed operation is told which step failed.

Each one is chosen through configuration (see :mod:`verticals`), never by editing code.
"""

import uuid

from calendar_contract import CalendarError, CreateEvent, Event, ReplaceEvent
from calendar_contract import ErrorCode as CalendarErrorCode
from cloud_storage_api import DocumentStorage, FileInfo
from cloud_storage_api.errors import ConflictError, NotFoundError
from issue_tracker_contract import ErrorCode as IssueErrorCode
from issue_tracker_contract import Issue, IssueUpdate, ServiceError

__all__ = [
    "FailingCalendar",
    "FailingIssueTracker",
    "InMemoryCalendar",
    "InMemoryDocumentStorage",
    "InMemoryIssueTracker",
]


def _new_id() -> str:
    return uuid.uuid4().hex


class InMemoryIssueTracker:
    """An ``IssueProvider`` that keeps issues in memory."""

    def __init__(self) -> None:
        """Start with no issues."""
        self._issues: dict[str, Issue] = {}

    def _existing(self, issue_id: str) -> Issue:
        try:
            return self._issues[issue_id]
        except KeyError:
            raise ServiceError(IssueErrorCode.NOT_FOUND, "Issue not found.") from None

    def get_issue(self, issue_id: str, *, fresh: bool = False) -> Issue:  # noqa: ARG002
        """Return the issue; ``fresh`` changes nothing, since nothing is cached."""
        return self._existing(issue_id)

    def update_issue(self, issue_id: str, changes: IssueUpdate) -> Issue:
        """Apply the supplied changes and return the updated issue."""
        issue = self._existing(issue_id).model_copy(update=changes.model_dump(exclude_unset=True))
        self._issues[issue_id] = issue
        return issue

    def delete_issue(self, issue_id: str) -> None:
        """Delete the issue, or raise ``not_found`` if it does not exist."""
        self._existing(issue_id)
        del self._issues[issue_id]

    def create_issue(self, title: str) -> Issue:
        """Create an issue with a new ID."""
        issue = Issue(id=_new_id(), title=title)
        self._issues[issue.id] = issue
        return issue

    def list_issues(self, *, limit: int) -> list[Issue]:
        """Return at most ``limit`` issues, newest first."""
        return list(reversed(self._issues.values()))[:limit]


class FailingIssueTracker:
    """An ``IssueProvider`` whose every call fails with ``provider_error``.

    ``may_have_applied=False``: the failure happens before anything is written, so a caller
    may safely treat the step as not done.
    """

    def _fail(self) -> ServiceError:
        return ServiceError(
            IssueErrorCode.PROVIDER_ERROR,
            "The issue tracker is unavailable (controlled failure).",
            may_have_applied=False,
        )

    def get_issue(self, issue_id: str, *, fresh: bool = False) -> Issue:  # noqa: ARG002
        """Fail."""
        raise self._fail()

    def update_issue(self, issue_id: str, changes: IssueUpdate) -> Issue:  # noqa: ARG002
        """Fail."""
        raise self._fail()

    def delete_issue(self, issue_id: str) -> None:  # noqa: ARG002
        """Fail."""
        raise self._fail()

    def create_issue(self, title: str) -> Issue:  # noqa: ARG002
        """Fail."""
        raise self._fail()

    def list_issues(self, *, limit: int) -> list[Issue]:  # noqa: ARG002
        """Fail."""
        raise self._fail()


class InMemoryCalendar:
    """A ``CalendarService`` that keeps events in memory."""

    def __init__(self) -> None:
        """Start with no events."""
        self._events: dict[str, Event] = {}

    def _existing(self, event_id: str) -> Event:
        try:
            return self._events[event_id]
        except KeyError:
            raise CalendarError(
                CalendarErrorCode.NOT_FOUND,
                "Event not found.",
                retryable=False,
            ) from None

    def create(self, request: CreateEvent) -> Event:
        """Create an event with a new ID."""
        event = Event(id=_new_id(), **request.model_dump())
        self._events[event.id] = event
        return event

    def get(self, event_id: str) -> Event:
        """Return the event, or raise ``not_found``."""
        return self._existing(event_id)

    def replace(self, event_id: str, request: ReplaceEvent) -> Event:
        """Replace the title, start and end, keeping the ID."""
        self._existing(event_id)
        event = Event(id=event_id, **request.model_dump())
        self._events[event_id] = event
        return event

    def delete(self, event_id: str) -> None:
        """Delete the event, or raise ``not_found`` if it does not exist."""
        self._existing(event_id)
        del self._events[event_id]


class FailingCalendar:
    """A ``CalendarService`` whose every call fails with one ``CalendarError``.

    By default the calendar is unavailable (``provider_unavailable``, retryable), as when
    Google Calendar is down; tests can choose another code.
    """

    def __init__(
        self,
        code: CalendarErrorCode = CalendarErrorCode.PROVIDER_UNAVAILABLE,
        *,
        retryable: bool = True,
    ) -> None:
        """Fail every call with ``code``."""
        self._code = code
        self._retryable = retryable

    def _fail(self) -> CalendarError:
        return CalendarError(
            self._code,
            "The calendar is unavailable (controlled failure).",
            retryable=self._retryable,
        )

    def create(self, request: CreateEvent) -> Event:  # noqa: ARG002
        """Fail."""
        raise self._fail()

    def get(self, event_id: str) -> Event:  # noqa: ARG002
        """Fail."""
        raise self._fail()

    def replace(self, event_id: str, request: ReplaceEvent) -> Event:  # noqa: ARG002
        """Fail."""
        raise self._fail()

    def delete(self, event_id: str) -> None:  # noqa: ARG002
        """Fail."""
        raise self._fail()


class InMemoryDocumentStorage(DocumentStorage):
    """Files in a dictionary, keyed by path. Folders exist implicitly, as parents of files."""

    def __init__(self) -> None:
        """Start with no files."""
        self.files: dict[str, bytes] = {}

    def _info(self, path: str) -> FileInfo:
        if path in self.files:
            name = path.rsplit("/", 1)[-1]
            return FileInfo(
                None,
                name,
                path,
                len(self.files[path]),
                "2026-01-01T00:00:00Z",
                None,
                "file",
            )
        if any(p.startswith(path.rstrip("/") + "/") for p in self.files):
            return FileInfo(None, path.rsplit("/", 1)[-1], path, None, None, None, "folder")
        raise NotFoundError(path)

    def create(self, path: str, content: bytes) -> FileInfo:
        """Create a file; ``ConflictError`` if one exists."""
        if path in self.files:
            raise ConflictError(path)
        self.files[path] = content
        return self._info(path)

    def replace(self, path: str, content: bytes) -> FileInfo:
        """Replace an existing file; ``NotFoundError`` if missing."""
        if path not in self.files:
            raise NotFoundError(path)
        self.files[path] = content
        return self._info(path)

    def download(self, path: str) -> bytes:
        """Return the file's bytes."""
        if path not in self.files:
            raise NotFoundError(path)
        return self.files[path]

    def get_metadata(self, path: str) -> FileInfo:
        """Return the file's or folder's info."""
        return self._info(path)

    def list_files(self, path: str) -> list[FileInfo]:
        """Return the direct children of a folder, sorted by path."""
        self._info(path)  # NotFoundError for a missing folder
        prefix = path.rstrip("/") + "/"
        children = {
            prefix + p[len(prefix) :].split("/", 1)[0] for p in self.files if p.startswith(prefix)
        }
        return [self._info(child) for child in sorted(children)]

    def move(self, from_path: str, to_path: str) -> FileInfo:
        """Move a file."""
        if to_path in self.files:
            raise ConflictError(to_path)
        self.files[to_path] = self.download(from_path)
        del self.files[from_path]
        return self._info(to_path)

    def delete(self, path: str) -> None:
        """Delete a file, or a folder and everything in it."""
        self._info(path)
        for p in [p for p in self.files if p == path or p.startswith(path.rstrip("/") + "/")]:
            del self.files[p]

    def check_health(self) -> None:
        """Report healthy: there is no provider to reach."""
