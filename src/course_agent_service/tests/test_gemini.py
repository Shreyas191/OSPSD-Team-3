"""``GeminiModel`` against a stub SDK client: the translation in both directions, and errors.

No test here reaches the network or needs an API key.
"""

from typing import Any

import pytest
from google.genai import errors, types

from course_agent_service.gemini import GeminiModel
from course_agent_service.model import Message, ModelError, ToolCall, ToolSpec

TOOL = ToolSpec(
    name="get_course",
    description="Read one course's deadlines.",
    parameters={
        "type": "object",
        "properties": {"course_id": {"type": "string"}},
        "required": ["course_id"],
    },
)


class StubModels:
    """Records each request and answers with a fixed response or raises a fixed error."""

    def __init__(self, result: types.GenerateContentResponse | Exception) -> None:
        """Hold the fixed result to return or raise."""
        self.result = result
        self.requests: list[dict[str, Any]] = []

    def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        """Record the request, then return or raise the fixed result."""
        self.requests.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StubClient:
    """Stands in for ``genai.Client``: only ``client.models`` is used."""

    def __init__(self, models: StubModels) -> None:
        """Expose ``models`` the way the SDK client does."""
        self.models = models


def response(
    *parts: types.Part,
    prompt_tokens: int = 12,
    output_tokens: int = 3,
) -> types.GenerateContentResponse:
    """Build a Gemini response with these parts and token counts."""
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
        ),
    )


def model_with(result: types.GenerateContentResponse | Exception) -> tuple[GeminiModel, StubModels]:
    """Build a ``GeminiModel`` whose SDK client answers with ``result``."""
    models = StubModels(result)
    return GeminiModel(
        api_key="unused",
        model_name="gemini-test",
        client=StubClient(models),
    ), models


def test_text_reply_and_token_usage_come_back() -> None:
    """Text reply and token usage come back."""
    model, _ = model_with(response(types.Part(text="Hello"), types.Part(text=" there")))

    reply = model.complete(system="Be brief.", messages=[Message(role="user", text="Hi")], tools=[])

    assert reply.text == "Hello there"
    assert reply.tool_calls == ()
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (12, 3)


def test_the_version_that_answered_comes_back() -> None:
    """The exact model version that answered comes back, for the run record."""
    answer = response(types.Part(text="Hi"))
    answer.model_version = "gemini-3.5-flash-lite-001"
    model, _ = model_with(answer)

    reply = model.complete(system="", messages=[Message(role="user", text="Hi")], tools=[])

    assert reply.model_version == "gemini-3.5-flash-lite-001"


def test_thoughts_are_not_shown_as_reply_text() -> None:
    """Thoughts are not shown as reply text."""
    model, _ = model_with(
        response(types.Part(text="private reasoning", thought=True), types.Part(text="Answer")),
    )

    reply = model.complete(system="", messages=[Message(role="user", text="Hi")], tools=[])

    assert reply.text == "Answer"


def test_a_function_call_becomes_a_tool_call() -> None:
    """A function call becomes a tool call."""
    call = types.FunctionCall(id="fc-1", name="get_course", args={"course_id": "cs101"})
    model, _ = model_with(response(types.Part(function_call=call)))

    reply = model.complete(system="", messages=[Message(role="user", text="CS101?")], tools=[TOOL])

    assert reply.tool_calls == (
        ToolCall(id="fc-1", name="get_course", arguments={"course_id": "cs101"}),
    )


def test_a_function_call_without_an_id_still_gets_one() -> None:
    """A function call without an id still gets one."""
    model, _ = model_with(
        response(types.Part(function_call=types.FunctionCall(name="get_course", args={}))),
    )

    reply = model.complete(system="", messages=[Message(role="user", text="x")], tools=[TOOL])

    assert reply.tool_calls[0].id.startswith("call-")


def test_the_request_carries_the_system_prompt_and_tools_with_automatic_calling_off() -> None:
    """The request carries the system prompt and tools with automatic calling off."""
    model, models = model_with(response(types.Part(text="ok")))

    model.complete(
        system="Only coursework.",
        messages=[Message(role="user", text="Hi")],
        tools=[TOOL],
    )

    request = models.requests[0]
    config: types.GenerateContentConfig = request["config"]
    assert request["model"] == "gemini-test"
    assert config.system_instruction == "Only coursework."
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True
    assert config.tools is not None
    declaration = config.tools[0].function_declarations[0]  # type: ignore[union-attr,index]
    assert (declaration.name, declaration.parameters_json_schema) == ("get_course", TOOL.parameters)


def test_no_tools_means_no_tool_config() -> None:
    """No tools means no tool config."""
    model, models = model_with(response(types.Part(text="ok")))

    model.complete(system="", messages=[Message(role="user", text="Hi")], tools=[])

    assert models.requests[0]["config"].tools is None


def test_a_conversation_with_tool_results_is_translated_turn_by_turn() -> None:
    """A conversation with tool results is translated turn by turn."""
    model, models = model_with(response(types.Part(text="Done")))
    call = ToolCall(id="fc-1", name="get_course", arguments={"course_id": "cs101"})
    history = [
        Message(role="user", text="CS101?"),
        Message(role="assistant", text="Looking.", tool_calls=(call,)),
        Message(role="tool", tool_call_id="fc-1", name="get_course", text='{"deadlines": []}'),
    ]

    model.complete(system="", messages=history, tools=[TOOL])

    user, assistant, tool = models.requests[0]["contents"]
    assert (user.role, user.parts[0].text) == ("user", "CS101?")
    assert assistant.role == "model"
    assert assistant.parts[0].text == "Looking."
    assert assistant.parts[1].function_call.args == {"course_id": "cs101"}
    assert tool.role == "user"
    assert tool.parts[0].function_response.id == "fc-1"
    assert tool.parts[0].function_response.response == {"result": '{"deadlines": []}'}


def test_geminis_own_turn_is_sent_back_unchanged() -> None:
    """Gemini's turn carries thought signatures; it must go back exactly as it came."""
    original = types.Content(
        role="model",
        parts=[types.Part(text="Looking.", thought_signature=b"sig")],
    )
    model, models = model_with(response(types.Part(text="Done")))

    model.complete(
        system="",
        messages=[
            Message(role="user", text="x"),
            Message(
                role="assistant",
                text="Looking.",
                provider_state=original.model_dump(mode="json", exclude_none=True),
            ),
        ],
        tools=[],
    )

    assert models.requests[0]["contents"][1] == original


def test_results_of_one_turns_calls_go_back_together() -> None:
    """Gemini wants every function response of a turn in one content."""
    model, models = model_with(response(types.Part(text="Done")))
    calls = (ToolCall("a", "list_courses", {}), ToolCall("b", "get_course_plan", {"course": "x"}))

    model.complete(
        system="",
        messages=[
            Message(role="user", text="x"),
            Message(role="assistant", tool_calls=calls),
            Message(role="tool", tool_call_id="a", name="list_courses", text="[]"),
            Message(role="tool", tool_call_id="b", name="get_course_plan", text="{}"),
            Message(role="user", text="thanks"),
        ],
        tools=[],
    )

    contents = models.requests[0]["contents"]
    assert len(contents) == 4
    assert [p.function_response.id for p in contents[2].parts or [] if p.function_response] == [
        "a",
        "b",
    ]


def test_the_reply_keeps_geminis_turn_for_the_next_call() -> None:
    """The reply keeps geminis turn for the next call."""
    model, _ = model_with(response(types.Part(text="ok")))

    reply = model.complete(system="", messages=[Message(role="user", text="x")], tools=[])

    # JSON, so it can be stored between turns, and it turns back into the same content.
    assert (types.Content.model_validate(reply.provider_state).parts or [])[0].text == "ok"


def test_an_empty_response_is_an_empty_reply() -> None:
    """An empty response is an empty reply."""
    model, _ = model_with(types.GenerateContentResponse(candidates=[]))

    reply = model.complete(system="", messages=[Message(role="user", text="x")], tools=[])

    assert (reply.text, reply.tool_calls, reply.usage.input_tokens) == ("", (), 0)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (400, False), (403, False)],
)
def test_api_errors_become_model_errors_without_provider_text(
    status: int,
    retryable: bool,  # noqa: FBT001 (pytest passes parametrized values positionally)
) -> None:
    """Api errors become model errors without provider text."""
    error = errors.APIError(
        status,
        {"error": {"code": status, "message": "secret detail", "status": "X"}},
    )
    model, _ = model_with(error)

    with pytest.raises(ModelError) as caught:
        model.complete(system="", messages=[Message(role="user", text="x")], tools=[])

    assert caught.value.retryable is retryable
    assert str(status) in str(caught.value)
    assert "secret detail" not in str(caught.value)


def test_a_network_failure_is_a_retryable_model_error() -> None:
    """A network failure is a retryable model error."""
    model, _ = model_with(ConnectionResetError("reset"))

    with pytest.raises(ModelError) as caught:
        model.complete(system="", messages=[Message(role="user", text="x")], tools=[])

    assert caught.value.retryable is True
    assert "ConnectionResetError" in str(caught.value)


def test_without_an_injected_client_the_sdk_client_is_built_from_the_key() -> None:
    """Without an injected client the sdk client is built from the key."""
    model = GeminiModel(api_key="test-key-not-real", model_name="gemini-test")

    assert model.model_name == "gemini-test"
