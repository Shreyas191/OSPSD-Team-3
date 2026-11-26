"""The agent service's HTTP app: conversation turns, run by the model loop (issue #92).

- ``POST /conversations``: start a conversation (stored by the API service).
- ``POST /conversations/{id}/turns``: run one student turn and return the reply, the stop
  reason, and the pending proposal if the turn produced one.

Every route reaches user data only through the API service (``API_BASE_URL``), forwarding
the caller's identity headers, so the API decides who the caller is. Nothing is kept in memory
between requests: a turn loads the conversation, runs, and appends what it produced.
Errors use the same body as the API service: ``{"error": {"code", "message"}}``.
"""

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from course_agent_service.api_client import ApiClient, ApiError, identity_headers
from course_agent_service.config import Settings, build_model
from course_agent_service.conversation import from_stored, to_stored
from course_agent_service.loop import Turn, run_turn
from course_agent_service.model import ModelClient
from course_agent_service.tools import Toolbox

logger = logging.getLogger(__name__)

API_TIMEOUT_SECONDS = 20.0
MAX_APPEND = 50  # the API's limit on messages per append
DISCONNECT_POLL_SECONDS = 0.5


def check_configuration() -> Settings:
    """Read the settings at startup, so a bad one stops the service before it serves."""
    return Settings.from_env()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the model and the API client once; each lives as long as the app."""
    settings = check_configuration()
    app.state.model = build_model(settings)
    app.state.api_http = (
        httpx.Client(base_url=settings.api_base_url, timeout=API_TIMEOUT_SECONDS)
        if settings.api_base_url
        else None
    )
    yield
    if app.state.api_http is not None:
        app.state.api_http.close()


app = FastAPI(
    title="Course Autopilot agent service",
    description="The model loop between the interface and the API service.",
    version="0.1.0",
    lifespan=lifespan,
)


class ServiceUnavailableError(Exception):
    """The agent isn't configured to do this: no model, or no API service."""

    def __init__(self, code: str, message: str) -> None:
        """Record the shared error code and message."""
        super().__init__(message)
        self.code = code
        self.message = message


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


@app.exception_handler(ServiceUnavailableError)
def _unavailable(_request: Request, exc: ServiceUnavailableError) -> JSONResponse:
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message)


@app.exception_handler(ApiError)
def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
    # The API's own status and code (401 not signed in, 404 not this user's, ...).
    return _error(exc.status, exc.code, exc.message)


def get_model(request: Request) -> ModelClient:
    """Return the configured model, or answer 503 when it's disabled."""
    model: ModelClient | None = request.app.state.model
    if model is None:
        code, msg = "assistant_disabled", "The assistant is turned off on this service"
        raise ServiceUnavailableError(code, msg)
    return model


def get_api_http(request: Request) -> httpx.Client:
    """Return the HTTP client for the API service, or answer 503 when there is none."""
    http: httpx.Client | None = request.app.state.api_http
    if http is None:
        code, msg = "api_not_configured", "API_BASE_URL is not set"
        raise ServiceUnavailableError(code, msg)
    return http


Model = Annotated[ModelClient, Depends(get_model)]
ApiHttp = Annotated[httpx.Client, Depends(get_api_http)]


class ConversationCreate(BaseModel):
    """Body of ``POST /conversations``."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)


class TurnRequest(BaseModel):
    """Body of ``POST /conversations/{id}/turns``: what the student wrote."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


class TurnResponse(BaseModel):
    """The turn's reply. ``proposal_id`` is set when a pending proposal is ready to review."""

    stop_reason: str
    reply: str
    proposal_id: str | None


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: answers without calling the model or the API service."""
    return {"status": "ok", "release": os.environ.get("RELEASE", "unknown")}


@app.post("/conversations", status_code=status.HTTP_201_CREATED)
def create_conversation(body: ConversationCreate, request: Request, http: ApiHttp) -> Any:  # noqa: ANN401
    """Start a conversation for the caller."""
    return ApiClient(http, identity_headers(request.headers)).create_conversation(body.title)


@app.post("/conversations/{conversation_id}/turns")
async def take_turn(
    conversation_id: str,
    body: TurnRequest,
    request: Request,
    model: Model,
    http: ApiHttp,
) -> TurnResponse:
    """Run one turn. If the caller disconnects, the turn stops at its next step."""
    api = ApiClient(http, identity_headers(request.headers))
    disconnected = threading.Event()
    finished = threading.Event()

    async def watch() -> None:
        while not finished.is_set():
            if await request.is_disconnected():
                disconnected.set()
                return
            await asyncio.sleep(DISCONNECT_POLL_SECONDS)

    watcher = asyncio.create_task(watch())
    try:
        turn = await run_in_threadpool(
            _turn,
            api,
            model,
            conversation_id,
            body.text,
            disconnected.is_set,
        )
    finally:
        finished.set()
        watcher.cancel()
    return TurnResponse(
        stop_reason=turn.stop_reason,
        reply=turn.reply,
        proposal_id=turn.proposal["id"] if turn.proposal else None,
    )


def _turn(
    api: ApiClient,
    model: ModelClient,
    conversation_id: str,
    text: str,
    cancelled: Callable[[], bool],
) -> Turn:
    history = [from_stored(m) for m in api.list_messages(conversation_id)]
    turn = run_turn(
        model=model,
        toolbox=Toolbox(api, conversation_id),
        history=history,
        text=text,
        cancelled=cancelled,
    )
    stored = [to_stored(m) for m in turn.messages[:-1]]
    stored.append(to_stored(turn.messages[-1], run=turn.record))
    for start in range(0, len(stored), MAX_APPEND):
        api.append_messages(conversation_id, stored[start : start + MAX_APPEND])
    _log(conversation_id, turn.record)
    return turn


def _log(conversation_id: str, record: dict[str, Any]) -> None:
    # One structured line per run: tool names and outcomes, timing, tokens, stop reason.
    # Never message text, document content, tool arguments or credentials.
    logger.info(
        json.dumps({"event": "agent_run", "conversation_id": conversation_id, **record}),
    )
