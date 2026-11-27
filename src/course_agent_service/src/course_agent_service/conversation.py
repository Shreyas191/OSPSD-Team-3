"""How a ``Message`` is stored: a JSON body in the API service's ``messages`` table.

The agent service keeps nothing in memory between turns. A turn loads the conversation
through the API, runs, and appends what it produced, so a restart loses nothing, including
a question waiting for the student's answer.
"""

from typing import Any

from course_agent_service.model import Message, ToolCall


def to_stored(message: Message, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the ``{"role", "content"}`` body that ``POST /conversations/{id}/messages`` takes."""
    content: dict[str, Any] = {"text": message.text}
    if message.tool_calls:
        content["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls
        ]
    if message.role == "tool":
        content["tool_call_id"] = message.tool_call_id
        content["name"] = message.name
    if message.provider_state is not None:
        content["provider_state"] = message.provider_state
    if run is not None:
        content["run"] = run
    return {"role": message.role, "content": content}


def from_stored(stored: dict[str, Any]) -> Message:
    """Return the ``Message`` a stored ``{"role", "content"}`` row holds."""
    content: dict[str, Any] = stored["content"]
    return Message(
        role=stored["role"],
        text=str(content.get("text", "")),
        tool_calls=tuple(
            ToolCall(id=c["id"], name=c["name"], arguments=dict(c["arguments"]))
            for c in content.get("tool_calls", [])
        ),
        tool_call_id=str(content.get("tool_call_id", "")),
        name=str(content.get("name", "")),
        provider_state=content.get("provider_state"),
    )
