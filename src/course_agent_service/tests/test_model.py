"""``ScriptedModel``: the test double the agent loop's deterministic tests are built on."""

import pytest

from course_agent_service.model import (
    Message,
    ModelError,
    ModelReply,
    ScriptedModel,
    ScriptExhaustedError,
    ToolCall,
    ToolSpec,
    Usage,
)

TOOL = ToolSpec(
    name="list_courses",
    description="List the student's courses.",
    parameters={"type": "object"},
)


def test_replies_come_back_in_order_and_every_call_is_recorded() -> None:
    """Replies come back in order and every call is recorded."""
    first = ModelReply(tool_calls=(ToolCall(id="c1", name="list_courses", arguments={}),))
    second = ModelReply(text="You have 2 courses.", usage=Usage(input_tokens=10, output_tokens=5))
    model = ScriptedModel([first, second])

    history = [Message(role="user", text="What courses do I have?")]
    assert model.complete(system="sys", messages=history, tools=[TOOL]) == first
    history += [
        first.as_message(),
        Message(role="tool", tool_call_id="c1", name="list_courses", text="[]"),
    ]
    assert model.complete(system="sys", messages=history, tools=[TOOL]) == second

    assert len(model.calls) == 2
    assert model.calls[0].system == "sys"
    assert model.calls[0].tools == (TOOL,)
    assert [m.role for m in model.calls[1].messages] == ["user", "assistant", "tool"]


def test_a_scripted_error_is_raised_instead_of_returned() -> None:
    """A scripted error is raised instead of returned."""
    model = ScriptedModel([ModelError("rate limited", retryable=True)])

    with pytest.raises(ModelError) as caught:
        model.complete(system="", messages=[], tools=[])

    assert caught.value.retryable is True


def test_calling_past_the_end_of_the_script_fails_the_test() -> None:
    """Calling past the end of the script fails the test."""
    model = ScriptedModel([])

    with pytest.raises(ScriptExhaustedError):
        model.complete(system="", messages=[], tools=[])


def test_a_reply_becomes_an_assistant_turn_with_its_tool_calls() -> None:
    """A reply becomes an assistant turn with its tool calls."""
    call = ToolCall(id="c1", name="list_courses", arguments={"limit": 5})
    reply = ModelReply(text="Checking.", tool_calls=(call,), provider_state="opaque")

    message = reply.as_message()

    assert message.role == "assistant"
    assert message.text == "Checking."
    assert message.tool_calls == (call,)
    assert message.provider_state == "opaque"
