"""Tests for choosing the issue-tracker and calendar implementations through configuration.

Building an implementation makes no network call, so these tests need no network and no
real credentials. Every test starts with none of the settings involved.
"""

from pathlib import Path

import pytest
from calendar_contract import CalendarError
from calendar_contract import ErrorCode as CalendarErrorCode
from fastapi import FastAPI, Request
from issue_tracker_contract import IssueProvider, ServiceError

from cloud_storage_service.app import check_configuration
from cloud_storage_service.configuration import ConfigurationError
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
from cloud_storage_service.verticals import build_calendar, build_issue_tracker

TRELLO = {
    "TRELLO_API_KEY": "fake-trello-key-value",
    "TRELLO_TOKEN": "fake-trello-token-value",
    "TRELLO_LIST_ID": "fake-trello-list-value",
}
SETTINGS = [
    "ISSUE_TRACKER_IMPL",
    "CALENDAR_IMPL",
    "CALENDAR_TOKEN_FILE",
    "K_SERVICE",
    *TRELLO,
]


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with none of the settings these builders read."""
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


# --- Defaults and controlled implementations ---


def test_both_default_to_the_in_memory_fakes() -> None:
    """With nothing set, the service runs on fakes, so it starts without any credential."""
    assert isinstance(build_issue_tracker(), InMemoryIssueTracker)
    assert isinstance(build_calendar(), InMemoryCalendar)


def test_blank_setting_means_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty value is treated like an unset one."""
    monkeypatch.setenv("ISSUE_TRACKER_IMPL", "  ")
    assert isinstance(build_issue_tracker(), InMemoryIssueTracker)


def test_failing_selects_the_failing_implementations(monkeypatch: pytest.MonkeyPatch) -> None:
    """``failing`` selects the controlled failures used to demonstrate step errors."""
    monkeypatch.setenv("ISSUE_TRACKER_IMPL", "failing")
    monkeypatch.setenv("CALENDAR_IMPL", "failing")
    assert isinstance(build_issue_tracker(), FailingIssueTracker)
    assert isinstance(build_calendar(), FailingCalendar)


@pytest.mark.parametrize("env_var", ["ISSUE_TRACKER_IMPL", "CALENDAR_IMPL"])
def test_failing_is_refused_on_the_demo_service(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
) -> None:
    """Controlled failures can never be switched on where users see the service."""
    monkeypatch.setenv("K_SERVICE", "cloud-storage-service-demo")
    monkeypatch.setenv(env_var, "failing")
    with pytest.raises(ConfigurationError, match=env_var):
        build_issue_tracker() if env_var == "ISSUE_TRACKER_IMPL" else build_calendar()


def test_failing_is_allowed_on_the_test_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test service may run a controlled failure."""
    monkeypatch.setenv("K_SERVICE", "cloud-storage-service-test")
    monkeypatch.setenv("CALENDAR_IMPL", "failing")
    assert isinstance(build_calendar(), FailingCalendar)


@pytest.mark.parametrize("env_var", ["ISSUE_TRACKER_IMPL", "CALENDAR_IMPL"])
def test_unknown_implementation_is_refused_and_named(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
) -> None:
    """A typo stops startup with the setting and the allowed values, not a later failure."""
    monkeypatch.setenv(env_var, "team99")
    with pytest.raises(ConfigurationError, match=rf"{env_var}.*'team99'.*fake"):
        build_issue_tracker() if env_var == "ISSUE_TRACKER_IMPL" else build_calendar()


# --- Team 4 (Trello) ---


def test_team4_builds_the_trello_provider_without_calling_trello(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the three Trello settings, ``team4`` builds Team 4's provider (no network)."""
    monkeypatch.setenv("ISSUE_TRACKER_IMPL", "team4")
    for name, value in TRELLO.items():
        monkeypatch.setenv(name, value)

    issue_tracker = build_issue_tracker()

    assert type(issue_tracker).__name__ == "TrelloIssueProvider"
    assert isinstance(issue_tracker, IssueProvider)


@pytest.mark.parametrize("missing", list(TRELLO))
def test_team4_names_a_missing_trello_setting_and_no_values(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """A missing Trello setting stops startup; the message names it and shows no value."""
    monkeypatch.setenv("ISSUE_TRACKER_IMPL", "team4")
    for name, value in TRELLO.items():
        if name != missing:
            monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=missing) as exc_info:
        build_issue_tracker()

    for value in TRELLO.values():
        assert value not in str(exc_info.value)


# --- Team 5 (Google Calendar) ---


def test_team5_builds_the_google_calendar_from_a_token_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With a token file, ``team5`` builds Team 5's service; it reads the file lazily."""
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    monkeypatch.setenv("CALENDAR_IMPL", "team5")
    monkeypatch.setenv("CALENDAR_TOKEN_FILE", str(token_file))

    calendar = build_calendar()

    assert type(calendar).__module__.startswith("team5_calendar")


def test_team5_without_a_token_file_setting_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``team5`` needs ``CALENDAR_TOKEN_FILE``."""
    monkeypatch.setenv("CALENDAR_IMPL", "team5")
    with pytest.raises(ConfigurationError, match="CALENDAR_TOKEN_FILE"):
        build_calendar()


def test_team5_with_a_missing_token_file_is_refused_without_the_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A path that names no file stops startup; the message does not repeat the path."""
    missing = str(tmp_path / "secret-dir" / "token.json")
    monkeypatch.setenv("CALENDAR_IMPL", "team5")
    monkeypatch.setenv("CALENDAR_TOKEN_FILE", missing)

    with pytest.raises(ConfigurationError, match="CALENDAR_TOKEN_FILE") as exc_info:
        build_calendar()

    assert missing not in str(exc_info.value)


# --- The failing implementations fail with the contracts' own errors ---


def test_failing_issue_tracker_raises_provider_error_that_wrote_nothing() -> None:
    """Every call raises ``provider_error`` and says nothing was written."""
    issue_tracker = FailingIssueTracker()
    with pytest.raises(ServiceError) as exc_info:
        issue_tracker.create_issue("Bug 123")
    assert exc_info.value.code == "provider_error"
    assert exc_info.value.may_have_applied is False


def test_failing_calendar_raises_retryable_provider_unavailable() -> None:
    """By default every call raises ``provider_unavailable``, retryable."""
    with pytest.raises(CalendarError) as exc_info:
        FailingCalendar().get("any-event")
    assert exc_info.value.code == CalendarErrorCode.PROVIDER_UNAVAILABLE
    assert exc_info.value.retryable is True


# --- Wiring: the startup check and the route dependencies ---


def test_startup_check_stops_on_a_bad_vertical_setting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``check_configuration`` (run by ``main.py``) also checks the two new settings."""
    for name in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "fake-value")
    monkeypatch.setenv("DROPBOX_TOKEN_FILE", str(tmp_path / "no-token.json"))
    monkeypatch.setenv("CALENDAR_IMPL", "team99")

    with pytest.raises(ConfigurationError, match="CALENDAR_IMPL"):
        check_configuration()


def test_routes_get_the_implementations_built_at_startup() -> None:
    """The dependencies hand routes the objects the app keeps on ``app.state``."""
    app = FastAPI()
    app.state.issue_tracker = InMemoryIssueTracker()
    app.state.calendar = InMemoryCalendar()
    app.state.document_storage = object()
    request = Request({"type": "http", "app": app})

    assert get_issue_tracker(request) is app.state.issue_tracker
    assert get_calendar(request) is app.state.calendar
    assert get_document_storage(request) is app.state.document_storage
