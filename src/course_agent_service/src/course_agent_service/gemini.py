"""``GeminiModel``: the real ``ModelClient``, backed by Google's Gemini API (``google-genai``).

Only this module imports the Gemini SDK. It translates our provider-neutral messages and
tool specs into Gemini's types and back, and turns every SDK failure into a ``ModelError``.
Automatic function calling is off: the agent loop, not the SDK, decides which tool calls
run, after validating them.
"""

import uuid
from collections.abc import Sequence
from typing import Any, Protocol

from google.genai import errors, types

from course_agent_service.model import Message, ModelError, ModelReply, ToolCall, ToolSpec, Usage

# HTTP statuses after which a later retry may succeed: rate limits and server errors.
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class _Models(Protocol):
    def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse: ...


class _Client(Protocol):
    @property
    def models(self) -> _Models: ...


class GeminiModel:
    """Call one Gemini model. ``client`` is injectable so tests never reach the network."""

    def __init__(self, *, api_key: str, model_name: str, client: _Client | None = None) -> None:
        """Bind the model name and an SDK client built from ``api_key``."""
        if client is None:
            from google import genai  # noqa: PLC0415 (only built when no client is injected)

            client = genai.Client(api_key=api_key)
        self._client = client
        self.model_name = model_name

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> ModelReply:
        """Send the conversation to Gemini and return its next turn."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[_declaration(t) for t in tools])]
            if tools
            else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=_contents(messages),
                config=config,
            )
        except errors.APIError as e:
            # The message names the status only; the SDK's text may include request details.
            msg = f"Gemini request failed with status {e.code}"
            raise ModelError(msg, retryable=e.code in _RETRYABLE_STATUSES) from None
        except OSError as e:
            msg = f"Gemini could not be reached ({type(e).__name__})"
            raise ModelError(msg, retryable=True) from None
        return _reply(response)


def _declaration(tool: ToolSpec) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=tool.name,
        description=tool.description,
        parameters_json_schema=tool.parameters,
    )


def _contents(messages: Sequence[Message]) -> list[types.Content]:
    contents: list[types.Content] = []
    for message in messages:
        content = _content(message)
        if message.role == "tool" and contents and _is_function_response(contents[-1]):
            # Gemini expects all results of one turn's calls together, in one content.
            contents[-1].parts = [*(contents[-1].parts or []), *(content.parts or [])]
        else:
            contents.append(content)
    return contents


def _is_function_response(content: types.Content) -> bool:
    return bool(content.parts) and all(p.function_response for p in content.parts or [])


def _content(message: Message) -> types.Content:
    if message.role == "assistant" and isinstance(message.provider_state, dict):
        # Gemini's own turn, unchanged, so its thought signatures go back with it.
        return types.Content.model_validate(message.provider_state)
    if message.role == "assistant":
        parts = [types.Part(text=message.text)] if message.text else []
        parts += [
            types.Part(function_call=types.FunctionCall(id=c.id, name=c.name, args=c.arguments))
            for c in message.tool_calls
        ]
        return types.Content(role="model", parts=parts)
    if message.role == "tool":
        response = types.FunctionResponse(
            id=message.tool_call_id,
            name=message.name,
            response={"result": message.text},
        )
        return types.Content(role="user", parts=[types.Part(function_response=response)])
    return types.Content(role="user", parts=[types.Part(text=message.text)])


def _reply(response: types.GenerateContentResponse) -> ModelReply:
    candidate = response.candidates[0] if response.candidates else None
    content = candidate.content if candidate else None
    parts = (content.parts or []) if content else []
    text = "".join(p.text for p in parts if p.text and not p.thought)
    calls = tuple(_tool_call(p.function_call) for p in parts if p.function_call)
    usage = response.usage_metadata
    return ModelReply(
        text=text,
        tool_calls=calls,
        usage=Usage(
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=(usage.candidates_token_count or 0) if usage else 0,
        ),
        # Stored as JSON between turns (signatures are base64), so a restart keeps them.
        provider_state=content.model_dump(mode="json", exclude_none=True) if content else None,
        model_version=response.model_version or "",
    )


def _tool_call(call: types.FunctionCall) -> ToolCall:
    args: dict[str, Any] = dict(call.args or {})
    # Gemini doesn't always assign IDs; the loop needs one to match each result to its call.
    return ToolCall(
        id=call.id or f"call-{uuid.uuid4().hex[:12]}",
        name=call.name or "",
        arguments=args,
    )
