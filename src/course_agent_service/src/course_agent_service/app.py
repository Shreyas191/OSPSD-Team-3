"""The agent service's HTTP app: conversation turns, run by the model loop (issue #92).

- ``POST /conversations``: start a conversation (stored by the API service).
- ``POST /conversations/{id}/turns``: run one student turn and return the reply, the stop
  reason, and the pending proposal if the turn produced one.

**Admission (#102).** At most ``MAX_RUNNING_TURNS`` (3) turns run at once and at most
``MAX_QUEUED_TURNS`` (10) wait for a slot; past that, or after waiting ``MAX_QUEUE_SECONDS``, a
turn answers ``429 assistant_busy`` with nothing stored. These hold across the service because
it runs on one instance (``infra/agent.tf``); the per-minute and per-day model-call limits are
counted by the API service, so they hold whatever the instance count. A turn's wait for a slot
and for the per-minute limit is reported as ``waited_ms``. A student with no model calls left
today gets ``429 daily_limit_reached`` before anything runs.

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
from collections.abc import AsyncIterator, Awaitable, Callable
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
from course_agent_service.loop import (
    BudgetDailyLimitError,
    BudgetRateLimitedError,
    BudgetUnavailableError,
    DailyLimitReachedError,
    Turn,
    run_turn,
)
from course_agent_service.model import ModelClient
from course_agent_service.tools import Toolbox

logger = logging.getLogger(__name__)

API_TIMEOUT_SECONDS = 20.0
MAX_APPEND = 50  # the API's limit on messages per append
DISCONNECT_POLL_SECONDS = 0.5
MAX_RUNNING_TURNS = 3
MAX_QUEUED_TURNS = 10
MAX_QUEUE_SECONDS = 60.0
BUSY = "The assistant is busy; try again in a minute"
# Yours or the whole project's (usage.py): the API says which, but either way it's for today.
DAILY_LIMIT = (
    "Today's limit for the assistant has been reached; your course plans and approvals still work"
)


class AssistantBusyError(Exception):
    """Every slot is taken and the queue is full, or the wait for a slot ran out."""


class Admission:
    """At most ``running`` turns at once; at most ``queued`` more wait for a slot."""

    def __init__(self, running: int = MAX_RUNNING_TURNS, queued: int = MAX_QUEUED_TURNS) -> None:
        """Start with every slot free."""
        self._slots = asyncio.Semaphore(running)
        self._queued = 0
        self._max_queued = queued

    @property
    def waiting(self) -> int:
        """How many turns are waiting for a slot."""
        return self._queued

    async def acquire(self, gone: Callable[[], Awaitable[bool]], patience: float) -> None:
        """Take a slot, waiting in the queue if needed.

        Raises:
            AssistantBusyError: The queue is full, or no slot came free within ``patience``.
            asyncio.CancelledError: The caller went away while waiting.

        """
        if not self._slots.locked():
            await self._slots.acquire()
            return
        if self._queued >= self._max_queued:
            raise AssistantBusyError
        self._queued += 1
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + patience
            while not await self._wait_a_moment(deadline - loop.time()):
                if loop.time() >= deadline:
                    raise AssistantBusyError
                if await gone():
                    raise asyncio.CancelledError
        finally:
            self._queued -= 1

    async def _wait_a_moment(self, left: float) -> bool:
        """Wait up to one poll interval for a slot; return whether one was taken."""
        try:
            await asyncio.wait_for(
                self._slots.acquire(),
                max(0.0, min(left, DISCONNECT_POLL_SECONDS)),
            )
        except TimeoutError:
            return False
        return True

    def release(self) -> None:
        """Give the slot back."""
        self._slots.release()


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


@app.exception_handler(AssistantBusyError)
def _busy(_request: Request, _exc: AssistantBusyError) -> JSONResponse:
    return _error(status.HTTP_429_TOO_MANY_REQUESTS, "assistant_busy", BUSY)


@app.exception_handler(DailyLimitReachedError)
def _daily_limit(_request: Request, _exc: DailyLimitReachedError) -> JSONResponse:
    return _error(status.HTTP_429_TOO_MANY_REQUESTS, "daily_limit_reached", DAILY_LIMIT)


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


def get_admission(request: Request) -> Admission:
    """Return the service's admission control, made on first use (one per running app)."""
    admission: Admission | None = getattr(request.app.state, "admission", None)
    if admission is None:
        admission = request.app.state.admission = Admission()
    return admission


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
    waited_ms: int = 0


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
    admission = get_admission(request)
    loop = asyncio.get_running_loop()
    queued_at = loop.time()
    await admission.acquire(request.is_disconnected, MAX_QUEUE_SECONDS)
    queued_ms = round((loop.time() - queued_at) * 1000)
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
            queued_ms,
        )
    finally:
        finished.set()
        watcher.cancel()
        admission.release()
    return TurnResponse(
        stop_reason=turn.stop_reason,
        reply=turn.reply,
        proposal_id=turn.proposal["id"] if turn.proposal else None,
        waited_ms=int(turn.record.get("waited_ms", 0)),
    )


def budget_for(api: ApiClient) -> Callable[[], None]:
    """Return the loop's budget: reserve each model call through the API service."""

    def reserve() -> None:
        try:
            api.reserve_model_call()
        except ApiError as e:
            if e.code == "daily_limit_reached":
                raise BudgetDailyLimitError from None
            if e.code == "model_rate_limited":
                raise BudgetRateLimitedError(e.retry_after or 1.0) from None
            raise BudgetUnavailableError from None

    return reserve


def _turn(  # noqa: PLR0913 (the turn's inputs, passed through the thread pool)
    api: ApiClient,
    model: ModelClient,
    conversation_id: str,
    text: str,
    cancelled: Callable[[], bool],
    queued_ms: int = 0,
) -> Turn:
    history = [from_stored(m) for m in api.list_messages(conversation_id)]
    turn = run_turn(
        model=model,
        toolbox=Toolbox(api, conversation_id),
        history=history,
        text=text,
        cancelled=cancelled,
        budget=budget_for(api),
    )
    turn.record["waited_ms"] = turn.record.get("waited_ms", 0) + queued_ms
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
