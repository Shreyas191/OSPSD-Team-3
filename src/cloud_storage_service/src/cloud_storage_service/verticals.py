"""Choose which team's implementation the service uses for the issue tracker and calendar.

Storage is always ours (``build_storage_client`` in ``app.py``). For the other two
verticals, one setting each picks the implementation, so switching teams is a configuration
change, not a code change:

``ISSUE_TRACKER_IMPL`` (default ``fake``)
    - ``team4``: Team 4's Trello implementation. Needs ``TRELLO_API_KEY``, ``TRELLO_TOKEN``
      and ``TRELLO_LIST_ID`` (our own Trello test board), which Team 4's package reads.
    - ``fake``: in memory, no provider.
    - ``failing``: every call fails (controlled failure).

``CALENDAR_IMPL`` (default ``fake``)
    - ``team5``: Team 5's Google Calendar implementation. Needs ``CALENDAR_TOKEN_FILE``, the
      path of the OAuth token file for our own Google test account.
    - ``fake``: in memory, no provider.
    - ``failing``: every call fails (controlled failure).

The rest of the service sees only the shared contract types, ``IssueProvider`` and
``CalendarService``; this module is the only one that names another team's package.

A mistake stops the service at startup with a :class:`ConfigurationError` that names the
setting, never a value: an unknown choice, a missing credential, or ``failing`` on the demo
service (controlled failures are for the test service only).
"""

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from calendar_contract import CalendarService
from issue_tracker_contract import IssueProvider

from cloud_storage_service.configuration import ConfigurationError
from cloud_storage_service.fake_verticals import (
    FailingCalendar,
    FailingIssueTracker,
    InMemoryCalendar,
    InMemoryIssueTracker,
)
from cloud_storage_service.faults import DEMO_SERVICE

__all__ = [
    "CALENDAR_ENV_VAR",
    "ISSUE_TRACKER_ENV_VAR",
    "build_calendar",
    "build_issue_tracker",
]

ISSUE_TRACKER_ENV_VAR = "ISSUE_TRACKER_IMPL"
CALENDAR_ENV_VAR = "CALENDAR_IMPL"
DEFAULT_IMPL = "fake"
FAILING_IMPL = "failing"

TRELLO_SETTINGS = ("TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_LIST_ID")
CALENDAR_TOKEN_ENV_VAR = "CALENDAR_TOKEN_FILE"  # noqa: S105 (a setting name, not a secret)


def _team4_trello() -> IssueProvider:
    missing = [name for name in TRELLO_SETTINGS if not os.environ.get(name)]
    if missing:
        msg = f"{ISSUE_TRACKER_ENV_VAR}=team4 needs {', '.join(missing)}"
        raise ConfigurationError(msg)
    # Imported here so the package is only loaded when it is chosen.
    from team_4_issue_tracker_trello import TrelloIssueProvider  # noqa: PLC0415

    return TrelloIssueProvider()


def _team5_google() -> CalendarService:
    token_file = os.environ.get(CALENDAR_TOKEN_ENV_VAR, "")
    if not token_file:
        msg = f"{CALENDAR_ENV_VAR}=team5 needs {CALENDAR_TOKEN_ENV_VAR}"
        raise ConfigurationError(msg)
    if not Path(token_file).is_file():
        msg = f"{CALENDAR_TOKEN_ENV_VAR} does not name a readable file"
        raise ConfigurationError(msg)
    from team5_calendar import create_calendar_service  # noqa: PLC0415

    return create_calendar_service(token_file=token_file)


ISSUE_TRACKERS: dict[str, Callable[[], IssueProvider]] = {
    "team4": _team4_trello,
    "fake": InMemoryIssueTracker,
    FAILING_IMPL: FailingIssueTracker,
}

CALENDARS: dict[str, Callable[[], CalendarService]] = {
    "team5": _team5_google,
    "fake": InMemoryCalendar,
    FAILING_IMPL: FailingCalendar,
}


def _choice(env_var: str, choices: Mapping[str, Callable[[], object]]) -> str:
    name = os.environ.get(env_var, "").strip() or DEFAULT_IMPL
    if name not in choices:
        msg = f"{env_var}: unknown implementation {name!r}; use one of {sorted(choices)}"
        raise ConfigurationError(msg)
    if name == FAILING_IMPL and os.environ.get("K_SERVICE") == DEMO_SERVICE:
        msg = f"{env_var}={FAILING_IMPL} is never allowed on {DEMO_SERVICE}"
        raise ConfigurationError(msg)
    return name


def build_issue_tracker() -> IssueProvider:
    """Build the issue tracker that ``ISSUE_TRACKER_IMPL`` selects.

    Makes no network call: a missing credential is caught here, a rejected one on first use.

    Raises:
        ConfigurationError: If the choice is unknown, not allowed here, or misses a setting.

    """
    return ISSUE_TRACKERS[_choice(ISSUE_TRACKER_ENV_VAR, ISSUE_TRACKERS)]()


def build_calendar() -> CalendarService:
    """Build the calendar that ``CALENDAR_IMPL`` selects.

    Makes no network call: a missing token file is caught here, a rejected one on first use.

    Raises:
        ConfigurationError: If the choice is unknown, not allowed here, or misses a setting.

    """
    return CALENDARS[_choice(CALENDAR_ENV_VAR, CALENDARS)]()
