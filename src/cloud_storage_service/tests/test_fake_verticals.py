"""The controlled implementations follow the shared contracts.

The calendar fakes run the calendar contract's own conformance suite, the same tests every
team's ``CalendarService`` must pass. The issue-tracker contract ships only an HTTP suite,
so the in-memory issue tracker is checked here directly against the ``IssueProvider`` rules.
"""

from collections.abc import Callable

import pytest
from calendar_contract import CalendarService
from calendar_contract import ErrorCode as CalendarErrorCode
from calendar_contract.conformance import (
    CalendarServiceConformance,
    FailingServiceFactory,
    ProviderFailure,
)
from issue_tracker_contract import IssueProvider, IssueUpdate, ServiceError

from cloud_storage_service.fake_verticals import (
    FailingCalendar,
    FailingIssueTracker,
    InMemoryCalendar,
    InMemoryIssueTracker,
)

# How the contract expects each provider failure to surface: (code, retryable).
_FAILURES = {
    ProviderFailure.ACCESS_DENIED: (CalendarErrorCode.ACCESS_DENIED, False),
    ProviderFailure.UNEXPECTED_RESPONSE: (CalendarErrorCode.PROVIDER_ERROR, False),
    ProviderFailure.UNAVAILABLE: (CalendarErrorCode.PROVIDER_UNAVAILABLE, True),
    ProviderFailure.UNCERTAIN_WRITE: (CalendarErrorCode.PROVIDER_ERROR, False),
}


class TestInMemoryCalendarConformance(CalendarServiceConformance):
    """``InMemoryCalendar`` and ``FailingCalendar`` pass the shared calendar suite."""

    @pytest.fixture
    def service(self) -> CalendarService:
        """Return the in-memory calendar under test."""
        return InMemoryCalendar()

    @pytest.fixture
    def failing_service(self) -> FailingServiceFactory:
        """Build a ``FailingCalendar`` raising what the contract expects for each failure."""

        def build(failure: ProviderFailure) -> CalendarService:
            code, retryable = _FAILURES[failure]
            return FailingCalendar(code, retryable=retryable)

        return build


@pytest.mark.parametrize("implementation", [InMemoryIssueTracker, FailingIssueTracker])
def test_issue_trackers_satisfy_the_issue_provider_protocol(
    implementation: type[IssueProvider],
) -> None:
    """Both fakes have every ``IssueProvider`` method."""
    assert isinstance(implementation(), IssueProvider)


def test_in_memory_issue_tracker_creates_reads_updates_and_deletes() -> None:
    """An issue goes through its whole life, and is gone afterwards."""
    tracker = InMemoryIssueTracker()

    issue = tracker.create_issue("Bug 123: login fails")
    assert tracker.get_issue(issue.id, fresh=True) == issue

    updated = tracker.update_issue(issue.id, IssueUpdate(title="Bug 123: fixed"))
    assert updated.id == issue.id
    assert tracker.get_issue(issue.id).title == "Bug 123: fixed"

    tracker.delete_issue(issue.id)
    with pytest.raises(ServiceError) as exc_info:
        tracker.get_issue(issue.id)
    assert exc_info.value.code == "not_found"


def test_in_memory_issue_tracker_gives_each_issue_its_own_id() -> None:
    """Two creations with the same title make two issues."""
    tracker = InMemoryIssueTracker()
    assert tracker.create_issue("Same").id != tracker.create_issue("Same").id


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.get_issue("missing"),
        lambda t: t.update_issue("missing", IssueUpdate(title="x")),
        lambda t: t.delete_issue("missing"),
    ],
    ids=["get", "update", "delete"],
)
def test_in_memory_issue_tracker_reports_a_missing_issue_as_not_found(
    call: Callable[[InMemoryIssueTracker], object],
) -> None:
    """Every operation on an unknown ID raises ``not_found``."""
    with pytest.raises(ServiceError) as exc_info:
        call(InMemoryIssueTracker())
    assert exc_info.value.code == "not_found"


def test_in_memory_issue_tracker_lists_newest_first_up_to_the_limit() -> None:
    """``list_issues`` returns the newest issues first and at most ``limit``."""
    tracker = InMemoryIssueTracker()
    first, second, third = (tracker.create_issue(t) for t in ("one", "two", "three"))

    assert tracker.list_issues(limit=2) == [third, second]
    assert tracker.list_issues(limit=10) == [third, second, first]


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.get_issue("x"),
        lambda t: t.update_issue("x", IssueUpdate(title="x")),
        lambda t: t.delete_issue("x"),
        lambda t: t.create_issue("x"),
        lambda t: t.list_issues(limit=1),
    ],
    ids=["get", "update", "delete", "create", "list"],
)
def test_failing_issue_tracker_fails_every_operation(
    call: Callable[[FailingIssueTracker], object],
) -> None:
    """Every ``FailingIssueTracker`` operation raises ``provider_error``."""
    with pytest.raises(ServiceError) as exc_info:
        call(FailingIssueTracker())
    assert exc_info.value.code == "provider_error"
