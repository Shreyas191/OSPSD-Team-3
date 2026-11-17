"""Controlled implementations of the issue-tracker and calendar contracts.

They let the service run, and the workflow be tested, without Trello or Google Calendar:

- :class:`InMemoryIssueTracker` and :class:`InMemoryCalendar` keep what they create in a
  dictionary for as long as the process runs. Unit tests and local development use them.
- :class:`FailingIssueTracker` and :class:`FailingCalendar` fail every call with the
  contract's own error, the way an unavailable provider would. They demonstrate that the
  caller of a composed operation is told which step failed.

Each one is chosen through configuration (see :mod:`verticals`), never by editing code.
"""

import uuid

from calendar_contract import CalendarError, CreateEvent, Event, ReplaceEvent
from calendar_contract import ErrorCode as CalendarErrorCode
from issue_tracker_contract import ErrorCode as IssueErrorCode
from issue_tracker_contract import Issue, IssueUpdate, ServiceError

__all__ = [
    "FailingCalendar",
    "FailingIssueTracker",
    "InMemoryCalendar",
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
