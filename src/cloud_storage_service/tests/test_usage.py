"""The model budget (#102, T12) and the execution switch, over HTTP and directly.

Each refusal has a control next to it: under the cap a call is allowed, and a refused
reservation counts nothing.
"""

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from course_autopilot_store import Database
from course_autopilot_store.models import UsageCounter, User
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from cloud_storage_service.app import app
from cloud_storage_service.configuration import ConfigurationError
from cloud_storage_service.database import get_database
from cloud_storage_service.dependencies import get_calendar, get_document_storage, get_issue_tracker
from cloud_storage_service.execution import EXECUTION_ENV_VAR
from cloud_storage_service.fake_verticals import (
    InMemoryCalendar,
    InMemoryDocumentStorage,
    InMemoryIssueTracker,
)
from cloud_storage_service.identity import DEV_SIGN_IN_ENV_VAR
from cloud_storage_service.usage import (
    PER_MINUTE_ENV_VAR,
    PER_USER_PER_DAY_ENV_VAR,
    DailyLimitError,
    Limits,
    RateLimitedError,
    day_start,
    reserve,
)

ALICE = {"X-Dev-User": "alice"}
BOB = {"X-Dev-User": "bob"}
ISSUE = {"op": "create_issue", "course": "CS-GY 6083", "title": "[CS-GY 6083] Midterm, Dec 10"}
# Nov 27, 2026, 15:00:20 UTC: 07:00 in Los Angeles (PST, UTC-8).
NOW = datetime(2026, 11, 27, 15, 0, 20, tzinfo=UTC)


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


@pytest.fixture
def issues() -> InMemoryIssueTracker:
    """Provide the issue tracker the API writes to."""
    return InMemoryIssueTracker()


@pytest.fixture
def http(
    database: Database,
    issues: InMemoryIssueTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Serve the API with dev sign-in on and the default limits."""
    monkeypatch.setenv(DEV_SIGN_IN_ENV_VAR, "enabled")
    monkeypatch.delenv("K_SERVICE", raising=False)
    for name in (PER_USER_PER_DAY_ENV_VAR, PER_MINUTE_ENV_VAR, EXECUTION_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_document_storage] = InMemoryDocumentStorage
    app.dependency_overrides[get_issue_tracker] = lambda: issues
    app.dependency_overrides[get_calendar] = InMemoryCalendar
    yield TestClient(app)
    app.dependency_overrides.clear()


def user(database: Database) -> uuid.UUID:
    """Create a user and return their ID."""
    with database.session() as session:
        row = User(auth_provider="dev", auth_subject=uuid.uuid4().hex[:12])
        session.add(row)
        session.flush()
        return row.id


# ----- Over HTTP -----


def test_a_reservation_says_whats_left(http: TestClient) -> None:
    """Under the cap: 200 with the calls left today."""
    response = http.post("/usage/model-calls", headers=ALICE)

    assert response.status_code == 200
    assert response.json() == {"remaining_today": 24}


def test_the_26th_call_of_the_day_is_refused(
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T12: past 25 calls today, 429 with when it resets; another user is unaffected."""
    monkeypatch.setenv(PER_MINUTE_ENV_VAR, "100")
    allowed = [http.post("/usage/model-calls", headers=ALICE).status_code for _ in range(25)]

    refused = http.post("/usage/model-calls", headers=ALICE)
    usage = http.get("/usage/model-calls", headers=ALICE).json()

    assert allowed == [200] * 25
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "daily_limit_reached"
    assert 0 < int(refused.headers["Retry-After"]) <= 24 * 3600
    assert usage["used_today"] == usage["limit_per_day"] == 25
    assert http.post("/usage/model-calls", headers=BOB).status_code == 200  # control


def test_the_projects_minute_is_shared_and_says_when_to_retry(
    http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the per-minute limit, any user waits; the refused call isn't counted for them."""
    monkeypatch.setenv(PER_MINUTE_ENV_VAR, "2")
    http.post("/usage/model-calls", headers=ALICE)
    http.post("/usage/model-calls", headers=BOB)

    refused = http.post("/usage/model-calls", headers=ALICE)

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "model_rate_limited"
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert http.get("/usage/model-calls", headers=ALICE).json()["used_today"] == 1


def test_the_budget_needs_a_signed_in_user(http: TestClient) -> None:
    """No identity: 401, and nothing is counted."""
    assert http.post("/usage/model-calls").status_code == 401


def test_bad_limits_stop_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """A limit that isn't a positive whole number is a configuration error naming it."""
    for value in ("0", "-3", "lots"):
        monkeypatch.setenv(PER_USER_PER_DAY_ENV_VAR, value)
        with pytest.raises(ConfigurationError, match=PER_USER_PER_DAY_ENV_VAR):
            Limits.from_env()
    monkeypatch.setenv(PER_USER_PER_DAY_ENV_VAR, "40")
    monkeypatch.setenv(PER_MINUTE_ENV_VAR, "30")
    assert Limits.from_env() == Limits(per_user_per_day=40, per_minute=30)


# ----- The counters themselves -----


def test_a_new_minute_allows_calls_again(database: Database) -> None:
    """The project's count starts again each minute."""
    uid, limits = user(database), Limits(per_user_per_day=25, per_minute=1)
    reserve(database, uid, limits, NOW)

    with pytest.raises(RateLimitedError) as raised:
        reserve(database, uid, limits, NOW)

    assert raised.value.retry_after == 40  # 15:00:20 → 15:01:00
    assert reserve(database, uid, limits, NOW + timedelta(seconds=40)) == 23


def test_the_day_starts_at_midnight_pacific(database: Database) -> None:
    """07:59 UTC on Nov 28 is still Nov 27 in Los Angeles; 08:00 UTC is a new day."""
    uid, limits = user(database), Limits(per_user_per_day=1, per_minute=100)
    late = datetime(2026, 11, 28, 7, 59, tzinfo=UTC)
    reserve(database, uid, limits, late)

    with pytest.raises(DailyLimitError) as raised:
        reserve(database, uid, limits, late + timedelta(seconds=30))

    assert raised.value.resets_at == datetime(2026, 11, 28, 8, 0, tzinfo=UTC)
    assert reserve(database, uid, limits, datetime(2026, 11, 28, 8, 0, tzinfo=UTC)) == 0
    assert day_start(late) == datetime(2026, 11, 27, 8, 0, tzinfo=UTC)


def test_old_counters_are_removed(database: Database) -> None:
    """Counters from more than two days ago are deleted on the next reservation."""
    uid, limits = user(database), Limits()
    reserve(database, uid, limits, NOW - timedelta(days=3))

    reserve(database, uid, limits, NOW)

    with database.session() as session:
        windows = session.scalars(select(UsageCounter.window_start)).all()
    assert all(w.replace(tzinfo=UTC) >= NOW - timedelta(days=2) for w in windows)


def test_many_callers_at_once_never_pass_the_limit(tmp_path: Path) -> None:
    """Twenty threads (standing in for agent instances) racing for 15 calls get exactly 15."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/race.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    database = Database(engine)
    database.upgrade()
    users = [user(database) for _ in range(20)]
    limits = Limits(per_user_per_day=25, per_minute=15)
    results: list[str] = []
    start = threading.Barrier(len(users))

    def call(uid: uuid.UUID) -> None:
        start.wait()
        try:
            reserve(database, uid, limits, NOW)
            results.append("ok")
        except RateLimitedError:
            results.append("wait")

    threads = [threading.Thread(target=call, args=(uid,)) for uid in users]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    engine.dispose()

    assert results.count("ok") == 15
    assert results.count("wait") == 5


# ----- The execution switch -----


def approved(http: TestClient) -> dict[str, Any]:
    """Propose and approve one card; return the proposal."""
    proposal: dict[str, Any] = http.post(
        "/proposals",
        json={"changes": [ISSUE]},
        headers=ALICE,
    ).json()
    http.post(
        f"/proposals/{proposal['id']}/approve",
        json={"changes_hash": proposal["changes_hash"]},
        headers=ALICE,
    )
    return proposal


def test_the_execution_switch_pauses_runs_and_keeps_approvals(
    http: TestClient,
    issues: InMemoryIssueTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EXECUTION=disabled: 503, nothing written, still approved; reads work (control: on runs)."""
    proposal = approved(http)
    monkeypatch.setenv(EXECUTION_ENV_VAR, "disabled")

    paused = http.post(f"/proposals/{proposal['id']}/execute", headers=ALICE)
    read = http.get(f"/proposals/{proposal['id']}", headers=ALICE)

    assert paused.status_code == 503
    assert paused.json()["error"]["code"] == "execution_disabled"
    assert read.json()["status"] == "approved"
    assert issues.list_issues(limit=10) == []
    monkeypatch.delenv(EXECUTION_ENV_VAR)
    assert http.post(f"/proposals/{proposal['id']}/execute", headers=ALICE).json()["status"] == (
        "done"
    )
