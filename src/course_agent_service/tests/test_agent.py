"""The agent's tools (#91), loop (#92) and HTTP routes, with a scripted model and a fake API.

``FakeApi`` answers the API service's routes from a table and records every request, so each
test can check exactly what reached the API, and in particular that a refused tool call sent
nothing at all.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from course_agent_service import app as agent_app
from course_agent_service.api_client import ApiClient
from course_agent_service.conversation import from_stored, to_stored
from course_agent_service.loop import STOP_MESSAGES, Limits, run_turn
from course_agent_service.model import (
    Message,
    ModelError,
    ModelReply,
    ScriptedModel,
    ToolCall,
    Usage,
)
from course_agent_service.tools import DOCUMENT_END, DOCUMENT_START, TOOL_NAMES, TOOL_SPECS, Toolbox

ALICE = {"x-dev-user": "alice"}
COURSE = "CS-GY 6083"
SYLLABUS = "Midterm: Week 7. Final project due Dec 15."
ZONE = "America/New_York"
EVENT = {
    "op": "create_event",
    "title": "Midterm",
    "start": "2026-10-15T14:00",
    "end": "2026-10-15T15:30",
}
CARD = {"op": "create_issue", "title": "[CS-GY 6083] Midterm, Oct 15"}


class FakeApi:
    """The API service's routes, answered from ``routes``; conversations are kept in memory."""

    def __init__(self) -> None:
        """Answer the read routes with a small course."""
        self.requests: list[httpx.Request] = []
        self.conversations: dict[str, list[dict[str, Any]]] = {"c1": []}
        self.routes: dict[tuple[str, str], tuple[int, Any]] = {
            ("GET", "/courses"): (200, [{"course": COURSE, "has_folder": True}]),
            ("GET", f"/courses/{COURSE}/documents"): (200, [{"name": "syllabus.pdf"}]),
            ("GET", f"/courses/{COURSE}/documents/syllabus.pdf"): (
                200,
                {
                    "course": COURSE,
                    "name": "syllabus.pdf",
                    "text": SYLLABUS,
                    "characters": len(SYLLABUS),
                    "truncated": False,
                },
            ),
            ("GET", f"/courses/{COURSE}/plan"): (200, {"course": COURSE, "items": []}),
        }
        self.proposal_status = 201

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request."""
        self.requests.append(request)
        key = (request.method, request.url.path)
        if request.url.path == "/proposals":
            body = json.loads(request.content)
            if self.proposal_status != 201:
                error = {"code": "invalid_request", "message": "Change 0 targets an event"}
                return httpx.Response(self.proposal_status, json={"error": error})
            proposal = {"id": "p1", "status": "pending", "changes": body["changes"]}
            return httpx.Response(201, json={**proposal, "changes_hash": "0" * 64})
        if request.url.path.startswith("/conversations"):
            return self._conversation(request)
        status, body = self.routes.get(key, (404, {"error": {"code": "not_found", "message": "x"}}))
        return httpx.Response(status, json=body)

    def _conversation(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        if len(parts) == 2:  # POST /conversations
            self.conversations["c2"] = []
            return httpx.Response(201, json={"id": "c2", "title": "", "created_at": "x"})
        stored = self.conversations.get(parts[2])
        if stored is None:
            error = {"code": "not_found", "message": "Conversation not found"}
            return httpx.Response(404, json={"error": error})
        if request.method == "GET":
            return httpx.Response(200, json=stored)
        stored.extend(json.loads(request.content)["messages"])
        return httpx.Response(201, json=[])

    def client(self) -> httpx.Client:
        """Return an HTTP client whose requests this fake answers."""
        return httpx.Client(base_url="http://api", transport=httpx.MockTransport(self.handle))

    def paths(self) -> list[str]:
        """Return the paths requested so far, conversations excluded."""
        return [
            f"{r.method} {r.url.path}"
            for r in self.requests
            if not r.url.path.startswith("/conversations")
        ]


@pytest.fixture
def api() -> FakeApi:
    """Return a fresh fake API."""
    return FakeApi()


def toolbox(api: FakeApi) -> Toolbox:
    """Return the tools acting as alice, in conversation c1."""
    return Toolbox(ApiClient(api.client(), ALICE), "c1")


def call(tool: str, /, **arguments: Any) -> ToolCall:
    """Return a tool call with a fixed ID per tool."""
    return ToolCall(id=f"id-{tool}", name=tool, arguments=arguments)


def calls(*tool_calls: ToolCall) -> ModelReply:
    """Return a model turn that asks for these tool calls."""
    return ModelReply(tool_calls=tool_calls, usage=Usage(10, 5))


def say(text: str) -> ModelReply:
    """Return a model turn with text only."""
    return ModelReply(text=text, usage=Usage(10, 5))


# ----- Tools (#91) -----


def test_every_tool_has_a_schema_and_a_description() -> None:
    """Every tool has a schema and a description."""
    assert (
        {s.name for s in TOOL_SPECS}
        == TOOL_NAMES
        == {
            "list_courses",
            "list_course_documents",
            "read_document",
            "get_course_plan",
            "propose_changes",
        }
    )
    for spec in TOOL_SPECS:
        assert spec.description
        assert spec.parameters["type"] == "object"
        assert "user_id" not in json.dumps(spec.parameters)


@pytest.mark.parametrize(
    ("tool_call", "path"),
    [
        (call("list_courses"), "GET /courses"),
        (call("list_course_documents", course=COURSE), f"GET /courses/{COURSE}/documents"),
        (call("get_course_plan", course=COURSE), f"GET /courses/{COURSE}/plan"),
    ],
)
def test_read_tools_call_one_api_route_as_the_caller(
    api: FakeApi,
    tool_call: ToolCall,
    path: str,
) -> None:
    """Read tools call one api route as the caller."""
    result = toolbox(api).run(tool_call)

    assert result.outcome == "ok"
    assert api.paths() == [path]
    assert api.requests[0].headers["x-dev-user"] == "alice"


def test_read_document_marks_the_text_as_content(api: FakeApi) -> None:
    """Read document marks the text as content."""
    result = toolbox(api).run(call("read_document", course=COURSE, name="syllabus.pdf"))

    assert result.outcome == "ok"
    assert result.text.index(DOCUMENT_START) < result.text.index(SYLLABUS)
    assert result.text.index(SYLLABUS) < result.text.index(DOCUMENT_END)


def test_a_slash_in_an_argument_stays_inside_one_path_segment(api: FakeApi) -> None:
    """A slash in an argument stays inside one path segment."""
    toolbox(api).run(call("read_document", course=COURSE, name="../other/x.pdf"))

    assert api.requests[0].url.raw_path.endswith(b"/documents/..%2Fother%2Fx.pdf")


def test_an_unknown_tool_runs_nothing(api: FakeApi) -> None:
    """An unknown tool runs nothing."""
    result = toolbox(api).run(call("delete_everything"))

    assert result.outcome == "unknown_tool"
    assert "delete_everything" in result.text
    assert api.requests == []


@pytest.mark.parametrize(
    "tool_call",
    [
        call("list_course_documents"),
        call("list_course_documents", course=7),
        call("list_courses", user_id="bob"),
        call("read_document", course=COURSE),
        call("propose_changes", course=COURSE, time_zone=ZONE, changes=[]),
        call("propose_changes", course=COURSE, time_zone=ZONE, changes=[{"op": "drop_table"}]),
        call(
            "propose_changes",
            course=COURSE,
            time_zone=ZONE,
            changes=[{**EVENT, "start": "2026-10-15T14:00-04:00"}],
        ),
        call("propose_changes", course=COURSE, changes=[EVENT]),
        call("propose_changes", course=COURSE, time_zone="Eastern", changes=[EVENT]),
        call(
            "propose_changes",
            course=COURSE,
            time_zone=ZONE,
            changes=[{**EVENT, "start": "2026-03-08T02:30"}],
        ),
        call(
            "propose_changes",
            course=COURSE,
            time_zone=ZONE,
            changes=[{**EVENT, "end": "2026-11-01T01:30"}],
        ),
        call(
            "propose_changes",
            course=COURSE,
            time_zone=ZONE,
            changes=[{**CARD, "user_id": "bob"}],
        ),
        call(
            "propose_changes",
            course=COURSE,
            time_zone=ZONE,
            changes=[{"op": "write_document", "name": "x.sh"}],
        ),
    ],
)
def test_malformed_arguments_run_nothing(api: FakeApi, tool_call: ToolCall) -> None:
    """Malformed arguments run nothing."""
    result = toolbox(api).run(tool_call)

    assert result.outcome == "invalid_arguments"
    assert json.loads(result.text)["error"].startswith("Invalid arguments")
    assert api.requests == []


def test_propose_changes_stores_a_pending_proposal(api: FakeApi) -> None:
    """Propose changes stores a pending proposal."""
    result = toolbox(api).run(
        call("propose_changes", course=COURSE, time_zone=ZONE, changes=[EVENT, CARD]),
    )

    assert result.outcome == "ok"
    assert result.proposal is not None
    sent = json.loads(api.requests[0].content)
    assert sent["conversation_id"] == "c1"
    assert [c["course"] for c in sent["changes"]] == [COURSE, COURSE]
    assert sent["changes"][0]["start"] == "2026-10-15T14:00:00-04:00"
    assert json.loads(result.text)["status"] == "pending"


def test_code_not_the_model_works_out_each_offset(api: FakeApi) -> None:
    """Deadlines either side of the end of daylight saving time get their own offsets (A1)."""
    december = {**EVENT, "start": "2026-12-15T23:00", "end": "2026-12-15T23:59"}

    toolbox(api).run(
        call("propose_changes", course=COURSE, time_zone=ZONE, changes=[EVENT, december]),
    )

    sent = json.loads(api.requests[0].content)["changes"]
    assert [c["start"] for c in sent] == ["2026-10-15T14:00:00-04:00", "2026-12-15T23:00:00-05:00"]


def test_a_card_only_proposal_needs_no_time_zone(api: FakeApi) -> None:
    """A card-only proposal needs no time zone."""
    result = toolbox(api).run(call("propose_changes", course=COURSE, changes=[CARD]))

    assert result.outcome == "ok"


def test_a_refused_proposal_returns_the_apis_reason(api: FakeApi) -> None:
    """A refused proposal returns the API's reason."""
    api.proposal_status = 400

    result = toolbox(api).run(
        call("propose_changes", course=COURSE, time_zone=ZONE, changes=[CARD]),
    )

    assert result.outcome == "refused"
    assert "targets an event" in result.text


@pytest.mark.parametrize("status", [401, 503])
def test_sign_in_and_outage_errors_are_fatal(api: FakeApi, status: int) -> None:
    """Sign in and outage errors are fatal."""
    api.routes["GET", "/courses"] = (status, {"error": {"code": "x", "message": "nope"}})

    assert toolbox(api).run(call("list_courses")).outcome == "fatal"


def test_an_unreachable_api_is_fatal() -> None:
    """An unreachable API is fatal."""

    def refuse(_request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg)

    http = httpx.Client(base_url="http://api", transport=httpx.MockTransport(refuse))
    result = Toolbox(ApiClient(http, ALICE)).run(call("list_courses"))

    assert result.outcome == "fatal"
    assert "unreachable" in result.text


def test_a_non_json_error_still_has_a_message(api: FakeApi) -> None:
    """A non-JSON error still has a message."""
    api.routes["GET", "/courses"] = (502, None)

    result = toolbox(api).run(call("list_courses"))

    assert result.outcome == "refused"
    assert "502" in result.text


# ----- The loop (#92) -----


def turn(api: FakeApi, *replies: ModelReply | ModelError, **kwargs: Any) -> Any:
    """Run one turn with a scripted model and return it with the model."""
    model = ScriptedModel(replies)
    result = run_turn(
        model=model,
        toolbox=toolbox(api),
        history=kwargs.pop("history", []),
        text=kwargs.pop("text", "Set up CS-GY 6083"),
        **kwargs,
    )
    return result, model


def test_s1_from_a_request_to_a_stored_proposal(api: FakeApi) -> None:
    """S1 from a request to a stored proposal."""
    result, model = turn(
        api,
        calls(call("list_course_documents", course=COURSE)),
        calls(call("read_document", course=COURSE, name="syllabus.pdf")),
        calls(call("propose_changes", course=COURSE, time_zone=ZONE, changes=[EVENT, CARD])),
    )

    assert result.stop_reason == "proposal_ready"
    assert result.proposal["id"] == "p1"
    assert "Nothing is added until you approve" in result.reply
    assert [m.role for m in result.messages] == [
        "user",
        *["assistant", "tool"] * 3,
        "assistant",
    ]
    # Every result answers its own call.
    for asked, answered in zip(result.messages[1::2], result.messages[2::2], strict=False):
        if asked.tool_calls:
            assert answered.tool_call_id == asked.tool_calls[0].id
    assert [t["name"] for t in result.record["tools"]] == [
        "list_course_documents",
        "read_document",
        "propose_changes",
    ]
    assert len(model.calls) == 3
    assert model.calls[0].tools == TOOL_SPECS


def test_the_run_record_has_no_document_text_or_arguments(api: FakeApi) -> None:
    """The run record has no document text or arguments."""
    result, _ = turn(
        api,
        calls(call("read_document", course=COURSE, name="syllabus.pdf")),
        say("Done reading."),
    )

    record = json.dumps(result.record)
    assert SYLLABUS not in record
    assert "syllabus.pdf" not in record
    assert result.record["stop_reason"] == "replied"
    assert result.record["model_calls"] == 2
    assert result.record["input_tokens"] == 20


def test_the_run_record_names_the_model_version(api: FakeApi) -> None:
    """The run record names the model version that answered."""
    result, _ = turn(api, ModelReply(text="Hi", model_version="gemini-3.5-flash-lite-001"))

    assert result.record["model_version"] == "gemini-3.5-flash-lite-001"


def test_a_clarifying_question_ends_the_turn(api: FakeApi) -> None:
    """A clarifying question ends the turn."""
    question = "Week 7 starts Mon Oct 12. Is the midterm on Tue Oct 13 or Thu Oct 15?"
    result, _ = turn(
        api,
        calls(call("read_document", course=COURSE, name="syllabus.pdf")),
        say(question),
    )

    assert result.stop_reason == "replied"
    assert result.reply == question
    assert result.messages[-1].text == question
    assert "POST /proposals" not in api.paths()


def test_the_answer_continues_from_the_saved_question(api: FakeApi) -> None:
    """The answer continues from the saved question."""
    first, _ = turn(api, say("Is the midterm Oct 13 or Oct 15?"))

    second, model = turn(
        api,
        calls(call("propose_changes", course=COURSE, time_zone=ZONE, changes=[EVENT])),
        history=first.messages,
        text="Oct 15",
    )

    assert second.stop_reason == "proposal_ready"
    sent = [m.text for m in model.calls[0].messages]
    assert sent[:3] == ["Set up CS-GY 6083", "Is the midterm Oct 13 or Oct 15?", "Oct 15"]


def test_the_model_may_correct_an_invalid_proposal_twice(api: FakeApi) -> None:
    """The model may correct an invalid proposal twice."""
    bad = calls(
        call("propose_changes", course=COURSE, time_zone=ZONE, changes=[{"op": "create_event"}]),
    )
    good = calls(call("propose_changes", course=COURSE, time_zone=ZONE, changes=[EVENT]))

    result, _ = turn(api, bad, bad, good)

    assert result.stop_reason == "proposal_ready"


def test_a_third_invalid_proposal_stops_the_run(api: FakeApi) -> None:
    """A third invalid proposal stops the run."""
    api.proposal_status = 400
    refused = calls(call("propose_changes", course=COURSE, time_zone=ZONE, changes=[CARD]))

    result, model = turn(api, refused, refused, refused, say("unused"))

    assert result.stop_reason == "invalid_proposal"
    assert result.reply == STOP_MESSAGES["invalid_proposal"]
    assert len(model.calls) == 3


def test_an_unknown_tool_is_reported_and_the_turn_goes_on(api: FakeApi) -> None:
    """An unknown tool is reported and the turn goes on."""
    result, model = turn(api, calls(call("approve_proposal", id="p1")), say("I can't approve."))

    assert result.stop_reason == "replied"
    assert "no tool named" in model.calls[1].messages[-1].text
    assert api.paths() == []


def test_the_model_call_limit(api: FakeApi) -> None:
    """The model call limit."""
    loop = [calls(call("list_courses"))] * 5

    result, model = turn(api, *loop, limits=Limits(model_calls=3))

    assert result.stop_reason == "limit_model_calls"
    assert len(model.calls) == 3
    assert result.messages[-1].role == "assistant"


def test_the_time_limit(api: FakeApi) -> None:
    """The time limit."""
    now = iter([0.0, 0.0, 61.0, 61.0])

    result, model = turn(api, calls(call("list_courses")), say("x"), clock=lambda: next(now))

    assert result.stop_reason == "limit_time"
    assert len(model.calls) == 1


def test_the_token_limit(api: FakeApi) -> None:
    """The token limit."""
    big = ModelReply(tool_calls=(call("list_courses"),), usage=Usage(150_000, 60_000))

    result, _ = turn(api, big)

    assert result.stop_reason == "limit_tokens"
    assert api.paths() == []


@pytest.mark.parametrize(("retryable", "key"), [(True, "model_busy"), (False, "model_failed")])
def test_a_model_failure_stops_with_fixed_text(api: FakeApi, retryable: bool, key: str) -> None:  # noqa: FBT001
    """A model failure stops with fixed text."""
    result, _ = turn(api, ModelError("Gemini 429", retryable=retryable))

    assert result.stop_reason == "model_unavailable"
    assert result.reply == STOP_MESSAGES[key]
    assert "429" not in result.reply


def test_an_empty_reply(api: FakeApi) -> None:
    """An empty reply."""
    result, _ = turn(api, say("  "))

    assert result.stop_reason == "empty_reply"


def test_cancelled_before_the_first_model_call(api: FakeApi) -> None:
    """Cancelled before the first model call."""
    result, model = turn(api, say("x"), cancelled=lambda: True)

    assert result.stop_reason == "cancelled"
    assert model.calls == []


def test_cancelled_between_tool_calls_answers_every_call(api: FakeApi) -> None:
    """Cancelled between tool calls answers every call."""
    flags = iter([False, False, True, True, True])

    result, _ = turn(
        api,
        calls(call("list_courses"), call("get_course_plan", course=COURSE)),
        cancelled=lambda: next(flags),
    )

    assert result.stop_reason == "cancelled"
    tool_messages = [m for m in result.messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["id-list_courses", "id-get_course_plan"]
    assert api.paths() == ["GET /courses"]


def test_nothing_runs_after_a_stored_proposal(api: FakeApi) -> None:
    """Nothing runs after a stored proposal."""
    result, _ = turn(
        api,
        calls(
            call("propose_changes", course=COURSE, time_zone=ZONE, changes=[CARD]),
            call("list_courses"),
        ),
    )

    assert result.stop_reason == "proposal_ready"
    assert api.paths() == ["POST /proposals"]
    assert result.record["tools"][1]["outcome"] == "skipped"


def test_a_lost_api_stops_the_run(api: FakeApi) -> None:
    """A lost API stops the run."""
    api.routes["GET", "/courses"] = (401, {"error": {"code": "not_signed_in", "message": "x"}})

    result, model = turn(api, calls(call("list_courses")), say("unused"))

    assert result.stop_reason == "api_unavailable"
    assert len(model.calls) == 1


# ----- Stored messages -----


def test_messages_round_trip_through_storage() -> None:
    """Messages round trip through storage."""
    messages = [
        Message(role="user", text="hi"),
        Message(
            role="assistant",
            tool_calls=(call("list_courses"),),
            provider_state={"role": "model", "parts": [{"thought_signature": "AA=="}]},
        ),
        Message(role="tool", tool_call_id="id-list_courses", name="list_courses", text="[]"),
    ]

    assert [from_stored(json.loads(json.dumps(to_stored(m)))) for m in messages] == messages


# ----- HTTP routes -----


@pytest.fixture
def wire(api: FakeApi) -> Iterator[Callable[[list[ModelReply | ModelError]], TestClient]]:
    """Return a function that wires the agent app to the fake API and a scripted model."""

    def make(replies: list[ModelReply | ModelError]) -> TestClient:
        model = ScriptedModel(replies)
        agent_app.app.dependency_overrides[agent_app.get_model] = lambda: model
        agent_app.app.dependency_overrides[agent_app.get_api_http] = api.client
        return TestClient(agent_app.app)

    yield make
    agent_app.app.dependency_overrides.clear()


def test_a_turn_is_saved_with_its_run_record(api: FakeApi, wire: Any) -> None:
    """A turn is saved with its run record."""
    http = wire(
        [calls(call("propose_changes", course=COURSE, time_zone=ZONE, changes=[EVENT, CARD]))],
    )

    response = http.post("/conversations/c1/turns", json={"text": "Set up"}, headers=ALICE)

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "proposal_ready"
    assert response.json()["proposal_id"] == "p1"
    saved = api.conversations["c1"]
    assert [m["role"] for m in saved] == ["user", "assistant", "tool", "assistant"]
    assert saved[-1]["content"]["run"]["stop_reason"] == "proposal_ready"


def test_a_restart_keeps_the_question(api: FakeApi, wire: Any) -> None:
    """A restart keeps the question: the next turn, with a new model, sees it."""
    wire([say("Is the midterm Oct 13 or Oct 15?")]).post(
        "/conversations/c1/turns",
        json={"text": "Set up"},
        headers=ALICE,
    )
    model = ScriptedModel([say("Thanks, noted.")])
    agent_app.app.dependency_overrides[agent_app.get_model] = lambda: model

    TestClient(agent_app.app).post(
        "/conversations/c1/turns",
        json={"text": "Oct 15"},
        headers=ALICE,
    )

    assert [m.text for m in model.calls[0].messages] == [
        "Set up",
        "Is the midterm Oct 13 or Oct 15?",
        "Oct 15",
    ]


def test_only_identity_headers_reach_the_api(api: FakeApi, wire: Any) -> None:
    """Only identity headers reach the API."""
    response = wire([]).post(
        "/conversations",
        json={},
        headers={**ALICE, "cookie": "session=secret", "x-other": "1"},
    )

    assert response.status_code == 201
    sent = api.requests[0].headers
    assert sent["x-dev-user"] == "alice"
    assert "cookie" not in sent
    assert "x-other" not in sent


def test_another_users_conversation_is_404(api: FakeApi, wire: Any) -> None:
    """Another user's conversation is 404 (the fake API answers for the real one)."""
    response = wire([say("x")]).post(
        "/conversations/bobs/turns",
        json={"text": "hi"},
        headers=ALICE,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_an_empty_turn_is_422(api: FakeApi, wire: Any) -> None:
    """An empty turn is refused."""
    response = wire([]).post("/conversations/c1/turns", json={"text": ""}, headers=ALICE)

    assert response.status_code == 422


def test_a_disabled_assistant_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled assistant is 503, and so is a missing API_BASE_URL."""
    monkeypatch.setenv("MODEL_PROVIDER", "disabled")
    monkeypatch.setenv("API_BASE_URL", "http://api")
    with TestClient(agent_app.app) as http:
        response = http.post("/conversations/c1/turns", json={"text": "hi"}, headers=ALICE)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "assistant_disabled"

    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    monkeypatch.delenv("API_BASE_URL")
    with TestClient(agent_app.app) as http:
        response = http.post("/conversations", json={}, headers=ALICE)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "api_not_configured"
