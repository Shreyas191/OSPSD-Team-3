"""Failure analysis (#112): run key scenario S1 under each significant failure and record what happens.

Both services run in this process: the agent app's HTTP client is the API app's test client
(the same wiring as ``tests/test_agent_with_api.py``). The database is SQLite in memory, the
providers are the in-memory fakes, and the model is scripted, so nothing here touches the
network or needs credentials. Each case injects one failure and records what the student
would see and what state is left behind: the turn's stop reason, the HTTP statuses, the
proposal's status, each change's outcome, and how many events and cards exist.

It needs the agent loop (#92) and execution (#89, #90). Until they are merged, run it from a
checkout of the agent-loop branch::

    uv run python /path/to/scripts/failure_matrix.py

It prints one Markdown table row per case; docs/failure-analysis.md records a run.
"""

# ruff: noqa: T201, INP001 - a command-line script that prints its results

import json
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from calendar_contract import CalendarError, CreateEvent, Event
from calendar_contract import ErrorCode as CalendarCode
from cloud_storage_api.errors import ProviderUnavailableError
from cloud_storage_service.app import app as api_app
from cloud_storage_service.database import get_database
from cloud_storage_service.dependencies import get_calendar, get_document_storage, get_issue_tracker
from cloud_storage_service.fake_verticals import (
    InMemoryCalendar,
    InMemoryDocumentStorage,
    InMemoryIssueTracker,
)
from cloud_storage_service.identity import DEV_SIGN_IN_ENV_VAR
from course_agent_service.model import ModelError, ModelReply, ScriptedModel, ToolCall
from course_autopilot_store.models import Proposal
from fastapi.testclient import TestClient
from issue_tracker_contract import ErrorCode as IssueCode
from issue_tracker_contract import Issue, ServiceError
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from course_agent_service import app as agent_app
from course_autopilot_store import Database

ALICE = {"X-Dev-User": "alice"}
COURSE = "CS-GY 6083"
SYLLABUS = "CS-GY 6083 Database Systems\nMidterm: Thu Oct 15, 2-3:30 PM\n"
EVENT = {
    "op": "create_event",
    "title": "Midterm",
    "start": "2026-10-15T14:00:00-04:00",
    "end": "2026-10-15T15:30:00-04:00",
}
CARD = {"op": "create_issue", "title": "[CS-GY 6083] Midterm, Oct 15"}


def tool(name: str, /, **arguments: Any) -> ModelReply:  # noqa: ANN401
    """Return a model turn asking for one tool call."""
    return ModelReply(tool_calls=(ToolCall(id=f"id-{name}", name=name, arguments=arguments),))


# The model's side of a normal S1 turn: read the syllabus, then propose a card and an event.
S1_TURN = (
    tool("list_course_documents", course=COURSE),
    tool("read_document", course=COURSE, name="syllabus.md"),
    tool("propose_changes", course=COURSE, changes=[CARD, EVENT]),
)


# ----- Controlled failures -----


class UnavailableStorage(InMemoryDocumentStorage):
    """Dropbox is down: every call fails as unreachable."""

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401
        """Fail every public method."""
        if name.startswith("_") or name == "files":
            return super().__getattribute__(name)

        def fail(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
            msg = "Dropbox is unreachable (controlled failure)"
            raise ProviderUnavailableError(msg)

        return fail


class LostReplyCalendar(InMemoryCalendar):
    """Google Calendar makes the event, then the reply is lost (a timeout after the write)."""

    def create(self, request: CreateEvent) -> Event:
        """Create the event, then fail as if the reply never arrived."""
        super().create(request)
        msg = "The reply was lost (controlled failure)"
        raise CalendarError(CalendarCode.PROVIDER_ERROR, msg, retryable=False)


class UnreachableCalendar(InMemoryCalendar):
    """Google Calendar is down until ``recover()``."""

    def __init__(self) -> None:
        """Start unreachable."""
        super().__init__()
        self.down = True

    def create(self, request: CreateEvent) -> Event:
        """Fail before writing while down."""
        if self.down:
            msg = "The calendar is unreachable (controlled failure)"
            raise CalendarError(CalendarCode.PROVIDER_UNAVAILABLE, msg, retryable=True)
        return super().create(request)


class RateLimitedTracker(InMemoryIssueTracker):
    """Trello answers 429: refused before anything is written."""

    def create_issue(self, title: str) -> Issue:  # noqa: ARG002
        """Refuse."""
        msg = "Rate limited (controlled failure)"
        raise ServiceError(IssueCode.PROVIDER_ERROR, msg, provider_involved=True, may_have_applied=False)


class InstanceKilledError(BaseException):
    """Stands for the API instance stopping mid-request (Cloud Run timeout, crash, redeploy)."""


class KilledAfterWriteCalendar(InMemoryCalendar):
    """The event is written, then the instance stops before the outcome is saved."""

    def create(self, request: CreateEvent) -> Event:
        """Write, then stop."""
        super().create(request)
        raise InstanceKilledError


# ----- The world: both services, one database, the providers -----


@dataclass
class World:
    """The API app on an in-memory database and providers, and the agent app in front of it."""

    storage: Any = field(default_factory=InMemoryDocumentStorage)
    issues: Any = field(default_factory=InMemoryIssueTracker)
    calendar: Any = field(default_factory=InMemoryCalendar)
    database: Database | None = None
    api: TestClient = field(init=False)
    model: ScriptedModel = field(init=False)

    def __post_init__(self) -> None:
        """Upload alice's syllabus and start the API's test client."""
        self.storage.files[f"/Course Autopilot/{COURSE}/syllabus.md"] = SYLLABUS.encode()
        self.api = TestClient(api_app, raise_server_exceptions=False)
        self.model = ScriptedModel([])

    def agent(self, *replies: ModelReply | ModelError, api_http: Any = None) -> TestClient:  # noqa: ANN401
        """Return the agent app, whose next model replies are ``replies``."""
        self.model = ScriptedModel(replies)
        http = self.api if api_http is None else api_http
        agent_app.app.dependency_overrides[agent_app.get_model] = lambda: self.model
        agent_app.app.dependency_overrides[agent_app.get_api_http] = lambda: http
        return TestClient(agent_app.app, raise_server_exceptions=False)

    def counts(self) -> str:
        """How many events and cards exist in the providers."""
        events = len(getattr(self.calendar, "_events", {}))
        cards = len(getattr(self.issues, "_issues", {}))
        return f"{events} event(s), {cards} card(s)"


def proposal_count(w: World) -> int:
    """How many proposals are stored, whatever their status."""
    assert w.database is not None
    with w.database.session() as session:
        return len(session.scalars(select(Proposal)).all())


def in_memory_database() -> Database:
    """Return a fresh SQLite database with the latest schema."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    database = Database(engine)
    database.upgrade()
    return database


@contextmanager
def world(**providers: Any) -> Iterator[World]:  # noqa: ANN401
    """Wire both apps to a fresh database and the given providers."""
    os.environ[DEV_SIGN_IN_ENV_VAR] = "enabled"
    os.environ.pop("K_SERVICE", None)
    w = World(**providers)
    database = w.database or in_memory_database()
    w.database = database
    api_app.dependency_overrides[get_database] = lambda: database
    api_app.dependency_overrides[get_document_storage] = lambda: w.storage
    api_app.dependency_overrides[get_issue_tracker] = lambda: w.issues
    api_app.dependency_overrides[get_calendar] = lambda: w.calendar
    try:
        yield w
    finally:
        api_app.dependency_overrides.clear()
        agent_app.app.dependency_overrides.clear()


# ----- Steps of S1 -----


def start(agent: TestClient) -> tuple[int, str | None]:
    """Start a conversation; return the status and its ID."""
    r = agent.post("/conversations", json={"title": COURSE}, headers=ALICE)
    return r.status_code, (r.json().get("id") if r.status_code == 201 else None)  # noqa: PLR2004


def turn(agent: TestClient, cid: str) -> dict[str, Any]:
    """Run one student turn; return the status and the body."""
    r = agent.post(f"/conversations/{cid}/turns", json={"text": "Set up my course"}, headers=ALICE)
    return {"status": r.status_code, **r.json()}


def approve_and_execute(w: World, proposal_id: str) -> dict[str, Any]:
    """Approve the exact proposal and execute it; return the execute status and the proposal."""
    proposal = w.api.get(f"/proposals/{proposal_id}", headers=ALICE).json()
    w.api.post(
        f"/proposals/{proposal_id}/approve",
        json={"changes_hash": proposal["changes_hash"]},
        headers=ALICE,
    )
    return execute(w, proposal_id)


def execute(w: World, proposal_id: str) -> dict[str, Any]:
    """Execute; return the status and the proposal as the API reports it afterwards."""
    try:
        r = w.api.post(f"/proposals/{proposal_id}/execute", headers=ALICE)
        status = r.status_code
    except InstanceKilledError:
        status = "instance stopped"
    after = w.api.get(f"/proposals/{proposal_id}", headers=ALICE).json()
    return {"execute": status, "proposal": after}


def outcomes(proposal: dict[str, Any]) -> str:
    """Each change's outcome, in order."""
    items = proposal.get("outcomes") or []
    if not items:
        return "no outcomes saved"
    return ", ".join(f"{o.get('status')}" for o in items)


# ----- The cases -----


@dataclass
class Row:
    """One row of the table."""

    case: str
    student_sees: str
    state_after: str
    recovery: str


def baseline() -> Row:
    """No failure: the turn proposes, the student approves, both changes are made."""
    with world() as w:
        _, cid = start(w.agent(*S1_TURN))
        t = turn(w.agent(*S1_TURN), str(cid))
        done = approve_and_execute(w, t["proposal_id"])
        return Row(
            "none (baseline)",
            f"turn {t['status']} `{t['stop_reason']}`; execute {done['execute']}",
            f"proposal `{done['proposal']['status']}` ({outcomes(done['proposal'])}); {w.counts()}",
            "-",
        )


def model_down(*, retryable: bool) -> Row:
    """Gemini fails on the first call: quota or overload (retryable), or a bad key."""
    with world() as w:
        _, cid = start(w.agent())
        error = ModelError("controlled failure", retryable=retryable)
        t = turn(w.agent(error), str(cid))
        pending = proposal_count(w)
        return Row(
            f"Gemini {'quota/overload (429/503)' if retryable else 'rejects the key (401/403)'}",
            f'turn {t["status"]} `{t["stop_reason"]}`: "{t["reply"]}"',
            f"{pending} proposal(s); {w.counts()}",
            "send the message again" if retryable else "operator replaces the key",
        )


def api_unreachable() -> Row:
    """Run S1 when the agent can't reach the API service (down, or a network failure)."""
    with world() as w:
        dead = httpx.Client(base_url="http://127.0.0.1:9", timeout=2)
        r = w.agent(api_http=dead).post("/conversations", json={}, headers=ALICE)
        body = r.json()
        return Row(
            "API service unreachable from the agent",
            f"{r.status_code} `{body['error']['code']}`",
            f"nothing stored; {w.counts()}",
            "retry when the API is back",
        )


def database_down() -> Row:
    """Neon is unreachable: every query fails."""
    engine = create_engine("sqlite:////nonexistent-dir/db.sqlite")
    with world(database=Database(engine)) as w:
        health = w.api.get("/health/database")
        c_status, _ = start(w.agent())
        listing = w.api.get(f"/courses/{COURSE}/documents", headers=ALICE)
        code = health.json().get("error", {}).get("code")
        create_body = w.agent().post("/conversations", json={}, headers=ALICE).json()
        return Row(
            "Database (Neon) unreachable",
            f"start conversation {c_status} `{create_body.get('error', {}).get('code')}`; list documents {listing.status_code}",
            f"`/health/database` {health.status_code} `{code}`; nothing stored",
            "retry when Neon is back",
        )


def dropbox_down_during_turn() -> Row:
    """Dropbox is down while the agent reads the syllabus."""
    with world(storage=UnavailableStorage()) as w:
        _, cid = start(w.agent())
        t = turn(w.agent(*S1_TURN), str(cid))
        tools = [m for m in w.model.calls[-1].messages if m.role == "tool"] if w.model.calls else []
        return Row(
            "Dropbox down while reading documents",
            f'turn {t["status"]} `{t["stop_reason"]}`: "{t["reply"]}"',
            f"{len(w.model.calls)} model call(s), {len(tools)} tool result(s); {w.counts()}",
            "send the message again when Dropbox is back",
        )


def calendar_down_then_recovers() -> Row:
    """Google Calendar is down during execution; the card was already made. Then it recovers."""
    calendar = UnreachableCalendar()
    with world(calendar=calendar) as w:
        _, cid = start(w.agent(*S1_TURN))
        t = turn(w.agent(*S1_TURN), str(cid))
        first = approve_and_execute(w, t["proposal_id"])
        calendar.down = False
        again = execute(w, t["proposal_id"])
        return Row(
            "Google Calendar down during execution (before the write)",
            f"execute {first['execute']}: proposal `{first['proposal']['status']}` ({outcomes(first['proposal'])})",
            f"after recovery, execute again {again['execute']}: `{again['proposal']['status']}` "
            f"({outcomes(again['proposal'])}); {w.counts()}",
            "run again; only the failed change runs",
        )


def calendar_lost_reply() -> Row:
    """Run S1 when the event is created but the reply is lost."""
    with world(calendar=LostReplyCalendar()) as w:
        _, cid = start(w.agent(*S1_TURN))
        t = turn(w.agent(*S1_TURN), str(cid))
        first = approve_and_execute(w, t["proposal_id"])
        again = execute(w, t["proposal_id"])
        detail = (first["proposal"].get("outcomes") or [{}])[-1].get("detail")
        return Row(
            "Google Calendar write made, reply lost",
            f'execute {first["execute"]}: `{first["proposal"]["status"]}` ({outcomes(first["proposal"])}): "{detail}"',
            f"execute again {again['execute']}: `{again['proposal']['status']}`; {w.counts()}",
            "student checks the calendar; no automatic retry",
        )


def trello_rate_limited() -> Row:
    """Trello refuses the card with a rate limit; nothing was written."""
    with world(issues=RateLimitedTracker()) as w:
        _, cid = start(w.agent(*S1_TURN))
        t = turn(w.agent(*S1_TURN), str(cid))
        first = approve_and_execute(w, t["proposal_id"])
        detail = (first["proposal"].get("outcomes") or [{}])[-1].get("detail")
        return Row(
            "Trello rate limit (429) on the card",
            f'execute {first["execute"]}: `{first["proposal"]["status"]}` ({outcomes(first["proposal"])}): "{detail}"',
            f"later changes not run; {w.counts()}",
            "run again later",
        )


def api_instance_stops_mid_execution() -> Row:
    """Run S1 when the API instance stops after the calendar write, before saving its outcome."""
    with world(calendar=KilledAfterWriteCalendar()) as w:
        _, cid = start(w.agent(*S1_TURN))
        t = turn(w.agent(*S1_TURN), str(cid))
        first = approve_and_execute(w, t["proposal_id"])
        w.calendar.__class__ = InMemoryCalendar  # the next instance has a working calendar
        again = execute(w, t["proposal_id"])
        return Row(
            "API instance stops between a write and saving its outcome",
            f"execute: {first['execute']}; proposal `{first['proposal']['status']}` ({outcomes(first['proposal'])})",
            f"execute again {again['execute']}: `{again['proposal']['status']}`; {w.counts()}",
            "**none: stays `executing`**",
        )


CASES: list[Callable[[], Row]] = [
    baseline,
    lambda: model_down(retryable=True),
    lambda: model_down(retryable=False),
    api_unreachable,
    database_down,
    dropbox_down_during_turn,
    calendar_down_then_recovers,
    calendar_lost_reply,
    trello_rate_limited,
    api_instance_stops_mid_execution,
]


def main() -> None:
    """Run every case and print the table (and, with --json, the rows as JSON)."""
    rows = []
    for case in CASES:
        try:
            rows.append(case())
        except Exception as e:  # noqa: BLE001 (a case that crashes is itself a result)
            name = getattr(case, "__name__", "case")
            rows.append(Row(name, f"**the experiment crashed: {type(e).__name__}: {e}**", "", ""))
    if "--json" in sys.argv:
        print(json.dumps([r.__dict__ for r in rows], indent=2))
        return
    print("| Failure | What the student sees | State afterwards | Recovery |")
    print("|---|---|---|---|")
    for r in rows:
        print(f"| {r.case} | {r.student_sees} | {r.state_after} | {r.recovery} |")
    print(f"\n{len(rows)} cases, run id {uuid.uuid4().hex[:8]}")


if __name__ == "__main__":
    main()
