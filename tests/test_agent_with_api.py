"""The agent service against the real API service app, with a scripted model (#91, #92).

Nothing is faked between the two services except the network: the agent's HTTP client is the
API app's test client. So these tests check the whole path a turn takes: the agent's tools,
the API's identity, ownership and validation, the stored conversation, and approval and
execution on controlled providers. Both users' data live in one database (threats T2, T5).
"""

from collections.abc import Iterator
from typing import Any

import pytest
from calendar_contract import CreateEvent
from cloud_storage_service.app import app as api_app
from cloud_storage_service.database import get_database
from cloud_storage_service.dependencies import get_calendar, get_document_storage, get_issue_tracker
from cloud_storage_service.fake_verticals import (
    InMemoryCalendar,
    InMemoryDocumentStorage,
    InMemoryIssueTracker,
)
from cloud_storage_service.identity import DEV_SIGN_IN_ENV_VAR
from course_agent_service.model import ModelReply, ScriptedModel, ToolCall
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from course_agent_service import app as agent_app
from course_autopilot_store import Database

ALICE = {"X-Dev-User": "alice"}
BOB = {"X-Dev-User": "bob"}
COURSE = "CS-GY 6083"
SYLLABUS = "CS-GY 6083 Database Systems\nMidterm: Week 7 (in class)\nFinal project due Dec 15\n"


class World:
    """The API app on an in-memory database and providers, and a way to talk to the agent."""

    def __init__(self) -> None:
        """Start with alice's syllabus uploaded."""
        self.storage = InMemoryDocumentStorage()
        self.storage.files[f"/Course Autopilot/{COURSE}/syllabus.md"] = SYLLABUS.encode()
        self.issues = InMemoryIssueTracker()
        self.calendar = InMemoryCalendar()
        self.api = TestClient(api_app)
        self.model = ScriptedModel([])

    def agent(self, *replies: ModelReply) -> TestClient:
        """Return the agent app, whose next model replies are ``replies``."""
        self.model = ScriptedModel(replies)
        agent_app.app.dependency_overrides[agent_app.get_model] = lambda: self.model
        agent_app.app.dependency_overrides[agent_app.get_api_http] = lambda: self.api
        return TestClient(agent_app.app)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Wire both apps together."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    database = Database(engine)
    database.upgrade()
    monkeypatch.setenv(DEV_SIGN_IN_ENV_VAR, "enabled")
    monkeypatch.delenv("K_SERVICE", raising=False)
    w = World()
    api_app.dependency_overrides[get_database] = lambda: database
    api_app.dependency_overrides[get_document_storage] = lambda: w.storage
    api_app.dependency_overrides[get_issue_tracker] = lambda: w.issues
    api_app.dependency_overrides[get_calendar] = lambda: w.calendar
    yield w
    api_app.dependency_overrides.clear()
    agent_app.app.dependency_overrides.clear()
    engine.dispose()


def tool(name: str, /, **arguments: Any) -> ModelReply:
    """Return a model turn asking for one tool call."""
    return ModelReply(tool_calls=(ToolCall(id=f"id-{name}", name=name, arguments=arguments),))


EVENT = {"op": "create_event", "title": "Midterm", "start": "2026-10-15T14:00", "end": "2026-10-15T15:30"}
CARD = {"op": "create_issue", "title": "[CS-GY 6083] Midterm, Oct 15"}


def test_s1_question_then_proposal_then_approved_execution(world: World) -> None:
    """S1: read the syllabus, ask about the date, propose, then the person approves."""
    agent = world.agent(
        tool("list_course_documents", course=COURSE),
        tool("read_document", course=COURSE, name="syllabus.md"),
        ModelReply(text="Week 7 starts Oct 12. Is the midterm Tue Oct 13 or Thu Oct 15?"),
    )
    cid = agent.post("/conversations", json={"title": COURSE}, headers=ALICE).json()["id"]

    first = agent.post(f"/conversations/{cid}/turns", json={"text": "Set up my course"}, headers=ALICE)

    assert first.json()["stop_reason"] == "replied"
    # The syllabus reached the model as marked content.
    read_result = world.model.calls[2].messages[-1].text
    assert "Midterm: Week 7" in read_result
    assert "DOCUMENT CONTENT" in read_result

    # A new model (as after a restart) continues from the saved question.
    agent = world.agent(tool("propose_changes", course=COURSE, time_zone="America/New_York", changes=[EVENT, CARD]))
    second = agent.post(f"/conversations/{cid}/turns", json={"text": "Thursday"}, headers=ALICE)

    body = second.json()
    assert body["stop_reason"] == "proposal_ready"
    assert "Midterm: Week 7" in world.model.calls[0].messages[4].text
    # Nothing has changed in any provider yet.
    assert world.calendar._events == {}
    assert world.issues._issues == {}

    proposal = world.api.get(f"/proposals/{body['proposal_id']}", headers=ALICE).json()
    assert proposal["status"] == "pending"
    world.api.post(
        f"/proposals/{proposal['id']}/approve",
        json={"changes_hash": proposal["changes_hash"]},
        headers=ALICE,
    )
    done = world.api.post(f"/proposals/{proposal['id']}/execute", headers=ALICE).json()

    assert done["status"] == "done"
    assert [e.title for e in world.calendar._events.values()] == ["Midterm"]
    assert [i.title for i in world.issues._issues.values()] == [CARD["title"]]


def test_a_forced_delete_of_a_record_we_didnt_create_is_refused(world: World) -> None:
    """T5: a misled model that insists on deleting an event we didn't create changes nothing."""
    foreign = world.calendar.create(
        CreateEvent(title="Dentist", start="2026-10-16T09:00:00Z", end="2026-10-16T10:00:00Z"),
    ).id
    delete = {"op": "delete_event", "event_id": foreign}
    agent = world.agent(*[tool("propose_changes", course=COURSE, changes=[delete])] * 3)
    cid = agent.post("/conversations", json={}, headers=ALICE).json()["id"]

    body = agent.post(f"/conversations/{cid}/turns", json={"text": "Clean up"}, headers=ALICE).json()

    assert body["stop_reason"] == "invalid_proposal"
    assert body["proposal_id"] is None
    assert foreign in world.calendar._events
    # The API's reason went back to the model each time.
    assert "didn't create" in world.model.calls[1].messages[-1].text


def test_another_users_conversation_stays_hidden(world: World) -> None:
    """T2: bob can't continue alice's conversation, and the model never sees it."""
    agent = world.agent(ModelReply(text="hi"))
    cid = agent.post("/conversations", json={}, headers=ALICE).json()["id"]

    stolen = agent.post(f"/conversations/{cid}/turns", json={"text": "hi"}, headers=BOB)

    assert stolen.status_code == 404
    assert world.model.calls == []
    # Control: alice can.
    own = agent.post(f"/conversations/{cid}/turns", json={"text": "hi"}, headers=ALICE)
    assert own.status_code == 200


@pytest.mark.parametrize("headers", [{}, {"X-Dev-User": ""}, {"Authorization": "Bearer guess"}])
def test_no_session_spends_no_model_call(world: World, headers: dict[str, str]) -> None:
    """T12: the agent service is public, so a turn without a session is 401 before the model."""
    agent = world.agent(ModelReply(text="hi"))
    cid = agent.post("/conversations", json={}, headers=ALICE).json()["id"]

    anonymous = agent.post(f"/conversations/{cid}/turns", json={"text": "hi"}, headers=headers)

    assert anonymous.status_code == 401
    assert world.model.calls == []
    # Control: with a session, the same turn reaches the model.
    own = agent.post(f"/conversations/{cid}/turns", json={"text": "hi"}, headers=ALICE)
    assert own.status_code == 200
    assert len(world.model.calls) == 1


def test_a_turn_with_an_agent_token_proposes_but_cant_approve(world: World) -> None:
    """T14: the agent, holding only an agent token, gets a proposal stored but can't approve it."""
    token = world.api.post("/auth/agent-token", headers=ALICE).json()["token"]
    agent_token = {"Authorization": f"Bearer {token}"}
    agent = world.agent(tool("propose_changes", course=COURSE, changes=[CARD]))
    cid = agent.post("/conversations", json={}, headers=agent_token).json()["id"]

    body = agent.post(f"/conversations/{cid}/turns", json={"text": "Add it"}, headers=agent_token)

    assert body.json()["stop_reason"] == "proposal_ready"
    proposal = world.api.get(f"/proposals/{body.json()['proposal_id']}", headers=ALICE).json()
    approve = {"changes_hash": proposal["changes_hash"]}
    stolen = world.api.post(f"/proposals/{proposal['id']}/approve", json=approve, headers=agent_token)
    assert stolen.status_code == 403
    # Control: Alice's own session approves it.
    mine = world.api.post(f"/proposals/{proposal['id']}/approve", json=approve, headers=ALICE)
    assert mine.json()["status"] == "approved"
