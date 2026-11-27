"""The model interface: what the agent loop needs from a language model, and nothing more.

The loop talks only to ``ModelClient``. ``GeminiModel`` (gemini.py) is the real model;
``ScriptedModel`` replies from a list fixed in advance, so tests exercise the loop without a
network, an API key or quota. Keeping the provider behind this interface also lets us switch
providers by configuration if the free tier's limits change.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call: its name, when to use it, and a JSON Schema for its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One tool call the model proposed. ``id`` links the call to its result."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Tokens one model call used, for limits and cost reporting."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Message:
    """One turn of the conversation.

    - ``user``: the student's words (``text``).
    - ``assistant``: the model's reply: ``text``, ``tool_calls``, or both.
    - ``tool``: the result of one tool call (``tool_call_id``, ``name`` and ``text``).

    ``provider_state`` carries what a provider needs to continue its own turn (Gemini's
    thought signatures, for example). Only the provider that produced it reads it.
    """

    role: Role
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    name: str = ""
    provider_state: Any = None


@dataclass(frozen=True)
class ModelReply:
    """What one model call returned."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    provider_state: Any = None
    model_version: str = ""  # the exact version that answered, when the provider says

    def as_message(self) -> Message:
        """Return the reply as an ``assistant`` turn to append to the conversation."""
        return Message(
            role="assistant",
            text=self.text,
            tool_calls=self.tool_calls,
            provider_state=self.provider_state,
        )


class ModelError(RuntimeError):
    """The model call failed. ``retryable`` says whether trying again may help."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        """Record the failure and whether a retry may succeed."""
        super().__init__(message)
        self.retryable = retryable


class ModelClient(Protocol):
    """A language model the agent loop can call."""

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> ModelReply:
        """Return the model's next turn for this conversation.

        Raises ``ModelError`` when the call fails. Never raises a provider's own exception.
        """
        ...


class ScriptExhaustedError(AssertionError):
    """A test's ``ScriptedModel`` was called more times than it has replies."""


@dataclass(frozen=True)
class ModelCall:
    """One call a ``ScriptedModel`` received, recorded for test assertions."""

    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]


class ScriptedModel:
    """A ``ModelClient`` that returns replies fixed in advance, in order.

    A reply may be a ``ModelError`` instance, which is raised instead of returned. Every call
    is recorded in ``calls``, so a test can check exactly what the loop sent to the model.
    """

    def __init__(self, replies: Sequence[ModelReply | ModelError]) -> None:
        """Create a model that will answer with ``replies``, one per call."""
        self._replies = list(replies)
        self.calls: list[ModelCall] = []

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> ModelReply:
        """Record the call and return (or raise) the next scripted reply."""
        self.calls.append(ModelCall(system, tuple(messages), tuple(tools)))
        if not self._replies:
            msg = f"ScriptedModel has no reply left for call {len(self.calls)}"
            raise ScriptExhaustedError(msg)
        reply = self._replies.pop(0)
        if isinstance(reply, ModelError):
            raise reply
        return reply
