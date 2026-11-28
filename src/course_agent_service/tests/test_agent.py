"""The agent's tools (#91), loop (#92) and HTTP routes, with a scripted model and a fake API.

``FakeApi`` answers the API service's routes from a table and records every request, so each
test can check exactly what reached the API, and in particular that a refused tool call sent
nothing at all.
"""

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient

from course_agent_service import app as agent_app
from course_agent_service.api_client import ApiClient
from course_agent_service.conversation import from_stored, to_stored
from course_agent_service.loop import (
    SECRET_NOT_KEPT,
    STOP_MESSAGES,
    SYSTEM_PROMPT,
    BudgetDailyLimitError,
    BudgetRateLimitedError,
    BudgetUnavailableError,
    DailyLimitReachedError,
    Limits,
    run_turn,
)
from course_agent_service.model import (
    Message,
    ModelError,
    ModelReply,
    ScriptedModel,
    ToolCall,
    Usage,
)
from course_agent_service.tools import DOCUMENT_END, DOCUMENT_START, TOOL_NAMES, TOOL_SPECS, Toolbox

if TYPE_CHECKING:
    from pydantic import BaseModel

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
            ("POST", "/usage/model-calls"): (200, {"remaining_today": 20}),
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
        """Return the paths requested so far, conversations and model budget excluded."""
        return [
            f"{r.method} {r.url.path}"
            for r in self.requests
            if not r.url.path.startswith(("/conversations", "/usage"))
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


# ----- #102: the model budget and admission -----


class Budget:
    """A model budget that answers from a script: None (allowed) or an exception to raise."""

    def __init__(self, *answers: Exception | None) -> None:
        """Answer each reservation in turn; once the script runs out, allow every call."""
        self.answers = list(answers)
        self.asked = 0

    def __call__(self) -> None:
        """Reserve one call."""
        self.asked += 1
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            raise answer


def test_every_model_call_is_reserved_first(api: FakeApi) -> None:
    """The budget is asked once per model call, before it."""
    budget = Budget()

    result, model = turn(api, calls(call("list_courses")), say("Which course?"), budget=budget)

    assert result.stop_reason == "replied"
    assert budget.asked == len(model.calls) == 2


def test_a_full_minute_waits_for_the_next_one_and_reports_the_wait(api: FakeApi) -> None:
    """Over the per-minute limit, the turn waits and goes on; the wait is in the run record."""
    slept: list[float] = []
    budget = Budget(BudgetRateLimitedError(12), BudgetRateLimitedError(3))

    result, model = turn(api, say("Which course?"), budget=budget, sleep=slept.append)

    assert result.stop_reason == "replied"
    assert slept == [12, 3]
    assert result.record["waited_ms"] == 15_000
    assert len(model.calls) == 1


def test_a_wait_longer_than_the_turn_has_left_stops_it(api: FakeApi) -> None:
    """A wait past the turn's 60 s ends it with the time limit, and no model call."""
    budget = Budget(BudgetRateLimitedError(61))

    result, model = turn(api, say("x"), budget=budget, sleep=lambda _s: pytest.fail("no sleep"))

    assert result.stop_reason == "limit_time"
    assert model.calls == []


def test_leaving_while_waiting_stops_the_turn(api: FakeApi) -> None:
    """The browser going away during the wait stops the turn before the model call."""
    gone = iter([False, True])

    result, model = turn(
        api,
        say("x"),
        budget=Budget(BudgetRateLimitedError(5)),
        sleep=lambda _s: None,
        cancelled=lambda: next(gone),
    )

    assert result.stop_reason == "cancelled"
    assert model.calls == []


def test_no_calls_left_today_before_the_first_one_raises(api: FakeApi) -> None:
    """At the daily cap, the turn doesn't start: nothing is called or saved."""
    model = ScriptedModel([say("x")])

    with pytest.raises(DailyLimitReachedError):
        run_turn(
            model=model,
            toolbox=toolbox(api),
            history=[],
            text="Set up",
            budget=Budget(BudgetDailyLimitError()),
        )

    assert model.calls == []
    assert api.paths() == []


def test_running_out_partway_stops_with_a_message(api: FakeApi) -> None:
    """The cap reached after the first call ends the turn with fixed text."""
    budget = Budget(None, BudgetDailyLimitError())

    result, model = turn(api, calls(call("list_courses")), say("x"), budget=budget)

    assert result.stop_reason == "daily_limit"
    assert result.reply == STOP_MESSAGES["daily_limit"]
    assert len(model.calls) == 1


def test_a_budget_that_cant_be_asked_stops_the_turn(api: FakeApi) -> None:
    """The API refusing or being down for the budget ends the turn, with no model call."""
    result, model = turn(api, say("x"), budget=Budget(BudgetUnavailableError()))

    assert result.stop_reason == "api_unavailable"
    assert model.calls == []


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (429, "daily_limit_reached", BudgetDailyLimitError),
        (429, "model_rate_limited", BudgetRateLimitedError),
        (401, "not_signed_in", BudgetUnavailableError),
        (503, "database_unavailable", BudgetUnavailableError),
    ],
)
def test_the_api_budget_answers_become_the_loops(
    api: FakeApi,
    status: int,
    code: str,
    error: type[Exception],
) -> None:
    """Each API answer to a reservation maps to what the loop does."""
    api.routes[("POST", "/usage/model-calls")] = (
        status,
        {"error": {"code": code, "message": "x"}},
    )
    reserve = agent_app.budget_for(ApiClient(api.client(), ALICE))

    with pytest.raises(error):
        reserve()


def test_the_wait_comes_from_retry_after(api: FakeApi) -> None:
    """The per-minute wait is the API's ``Retry-After``."""
    error = {"error": {"code": "model_rate_limited", "message": "x"}}
    http = httpx.Client(
        base_url="http://api",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(429, json=error, headers={"Retry-After": "17"}),
        ),
    )

    with pytest.raises(BudgetRateLimitedError) as raised:
        agent_app.budget_for(ApiClient(http, ALICE))()

    assert raised.value.retry_after == 17


def test_at_the_daily_cap_a_turn_is_429_and_nothing_is_saved(api: FakeApi, wire: Any) -> None:
    """Over the cap: 429, no model call, nothing stored (control: under the cap, a reply)."""
    api.routes[("POST", "/usage/model-calls")] = (
        429,
        {"error": {"code": "daily_limit_reached", "message": "x"}},
    )
    http = wire([say("Hello")])

    response = http.post("/conversations/c1/turns", json={"text": "hi"}, headers=ALICE)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "daily_limit_reached"
    assert api.conversations["c1"] == []
    api.routes[("POST", "/usage/model-calls")] = (200, {"remaining_today": 1})
    control = http.post("/conversations/c1/turns", json={"text": "hi"}, headers=ALICE)
    assert control.json()["stop_reason"] == "replied"


def test_a_full_queue_is_429_busy(api: FakeApi, wire: Any) -> None:
    """No free slot and no room in the queue: 429, nothing stored."""
    http = wire([say("Hello")])
    agent_app.app.state.admission = agent_app.Admission(running=0, queued=0)
    try:
        response = http.post("/conversations/c1/turns", json={"text": "hi"}, headers=ALICE)
    finally:
        del agent_app.app.state.admission

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "assistant_busy"
    assert api.conversations["c1"] == []


async def _never_gone() -> bool:
    return False


async def _gone() -> bool:
    return True


def test_three_turns_run_at_once_and_the_rest_queue() -> None:
    """A fourth turn waits for a slot; it runs when one is released."""

    async def scenario() -> list[str]:
        admission = agent_app.Admission(running=3, queued=10)
        events: list[str] = []
        for _ in range(3):
            await admission.acquire(_never_gone, 1.0)
        fourth = asyncio.create_task(admission.acquire(_never_gone, 5.0))
        await asyncio.sleep(0.05)
        events.append(f"waiting={admission.waiting} done={fourth.done()}")
        admission.release()
        await asyncio.wait_for(fourth, 2.0)
        events.append(f"waiting={admission.waiting} done={fourth.done()}")
        return events

    assert asyncio.run(scenario()) == ["waiting=1 done=False", "waiting=0 done=True"]


def test_the_queue_has_room_for_ten() -> None:
    """With every slot taken and ten waiting, the eleventh is refused at once."""

    async def scenario() -> bool:
        admission = agent_app.Admission(running=1, queued=10)
        await admission.acquire(_never_gone, 1.0)
        waiting = [asyncio.create_task(admission.acquire(_never_gone, 5.0)) for _ in range(10)]
        await asyncio.sleep(0.05)
        try:
            await admission.acquire(_never_gone, 5.0)
        except agent_app.AssistantBusyError:
            refused = True
        else:
            refused = False
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        return refused and admission.waiting == 0

    assert asyncio.run(scenario())


def test_a_queued_turn_gives_up_after_its_wait_or_when_the_caller_leaves() -> None:
    """No slot within the wait → busy; the caller leaving → cancelled; neither holds a slot."""

    async def scenario() -> tuple[bool, bool, int]:
        admission = agent_app.Admission(running=1, queued=10)
        await admission.acquire(_never_gone, 1.0)
        try:
            await admission.acquire(_never_gone, 0.1)
            busy = False
        except agent_app.AssistantBusyError:
            busy = True
        try:
            await admission.acquire(_gone, 5.0)
            left = False
        except asyncio.CancelledError:
            left = True
        return busy, left, admission.waiting

    assert asyncio.run(scenario()) == (True, True, 0)


# ----- #102: what the assistant won't take in or give out (docs/acceptable-use.md) -----


@pytest.mark.parametrize(
    "text",
    [
        "my password is hunter2!!",
        "Dropbox token: sl.u.AbCdEfGhIjKlMnOpQrStUvWxYz0123",
        "here is the key AIzaSyA1234567890abcdefghijklmnop",
        "use ghp_abcdefghijklmnopqrstuvwxyz1234",
        "-----BEGIN RSA PRIVATE KEY----- MIIE",
    ],
)
def test_a_message_with_a_credential_never_reaches_the_model_or_the_database(
    api: FakeApi,
    wire: Any,
    text: str,
) -> None:
    """T10: no model call, no budget spent, and only a placeholder is stored."""
    http = wire([say("x")])

    response = http.post("/conversations/c1/turns", json={"text": text}, headers=ALICE)

    assert response.json()["stop_reason"] == "declined_secret"
    assert response.json()["reply"] == STOP_MESSAGES["declined_secret"]
    stored = json.dumps(api.conversations["c1"])
    assert text not in stored
    assert SECRET_NOT_KEPT in stored
    assert not any(r.url.path == "/usage/model-calls" for r in api.requests)


@pytest.mark.parametrize(
    "text",
    [
        "Set up CS-GY 6083 from my syllabus",
        "The midterm token deadline moved to Dec 10",
        "What's the late policy? Is the final 30%?",
        # Review on #127: course text that names a password or token but holds no secret.
        "The Zoom passcode: 834920, office hours on Tuesdays",
        "Lecture recordings password: posted on Brightspace",
        "The token is required for the lab printer",
        "Remind me that my password is due for a reset",
    ],
)
def test_ordinary_messages_go_through(api: FakeApi, text: str) -> None:
    """Control: course requests, even ones using words like "token", reach the model."""
    result, model = turn(api, say("Sure"), text=text)

    assert result.stop_reason == "replied"
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Your syllabus says: The Zoom passcode: 834920, office hours on Tuesdays",
        "Lecture recordings password: posted on Brightspace",
        "The token is required for the lab printer, so bring it to lab 3",
        "My notes say: password = hunter2b, but that's from my syllabus example",
    ],
)
def test_a_reply_quoting_course_text_is_shown(api: FakeApi, text: str) -> None:
    """A reply quoting the student's documents isn't blocked by words like "passcode"."""
    result, _ = turn(api, say(text))

    assert result.stop_reason == "replied"
    assert result.reply == text


def test_a_reply_with_a_credential_is_replaced(api: FakeApi) -> None:
    """T10: a model reply that looks like a key is never shown or stored."""
    result, _ = turn(api, say("Use this: sk-live_ABCDEFGHIJKLMNOPQRSTUV"))

    assert result.stop_reason == "blocked_output"
    assert result.reply == STOP_MESSAGES["blocked_output"]
    assert "sk-live" not in json.dumps([m.text for m in result.messages])


def test_the_instructions_carry_the_policy() -> None:
    """The prompt says what to decline and what to ask about (docs/acceptable-use.md)."""
    for phrase in ("doing coursework for them", "anything about another person", "Ask one short"):
        assert phrase in SYSTEM_PROMPT


# ----- #103: the tool says which fields each change takes, matching what's accepted -----


def test_the_propose_description_lists_exactly_each_ops_accepted_fields() -> None:
    """The fields the description gives each op are the fields its validation accepts.

    The S1 evaluation traced invalid proposals to the model guessing fields; the description
    now lists them, and this keeps the list from drifting from the models.
    """
    from course_agent_service import tools

    spec = next(s for s in TOOL_SPECS if s.name == "propose_changes")
    models: dict[str, type[BaseModel]] = {
        "create_event": tools.CreateEventArg,
        "update_event": tools.UpdateEventArg,
        "delete_event": tools.DeleteEventArg,
        "create_issue": tools.CreateIssueArg,
        "update_issue": tools.UpdateIssueArg,
        "delete_issue": tools.DeleteIssueArg,
        "write_document": tools.WriteDocumentArg,
    }
    for op, model in models.items():
        listed = spec.description.split(f"{op}: ", 1)[1].split(".", 1)[0]
        documented = {f.strip().removesuffix(" only") for f in listed.split(",")}
        accepted = set(model.model_fields) - {"op"}
        assert documented == accepted, op
