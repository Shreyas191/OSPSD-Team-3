"""The interface's HTTP app: server-rendered pages in front of the API service (DESIGN.md, P4).

This slice of #95 adds Google sign-in and sign-out, the session cookie, CSRF protection on
every form, and the signed-in course list. The interface decides nothing and stores nothing
but cookies: every check happens in the API service, which receives the person's session
token as ``Authorization: Bearer`` on every call.

Cookies (all ``HttpOnly``, ``SameSite=Lax`` and, when deployed, ``Secure``):

| Cookie | Holds | Lasts |
|---|---|---|
| ``ca_session`` | the session token from sign-in | 7 days, the session's longest life |
| ``ca_sign_in`` | the binding that ties a sign-in attempt to this browser | 10 minutes |
| ``ca_csrf`` | a random value every form must repeat (double-submit) | the browser session |
"""

import hmac
import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from course_web.backend import (
    AGENT_TIMEOUT_SECONDS,
    TIMEOUT_SECONDS,
    Agent,
    Api,
    BackendError,
    ChatEntry,
    Proposal,
    TrelloList,
)
from course_web.config import Settings
from course_web.views import STATUSES, change_views

logger = logging.getLogger(__name__)


class HideCallbackQueries(logging.Filter):
    """Keep sign-in and connection codes out of uvicorn's access log (T10).

    Google and Dropbox return ``state`` and ``code`` in the query of ``/auth/callback`` and
    ``/connections/<provider>/callback``. Each is single-use and tied to its session, but it
    still doesn't belong in a log, so those lines keep the path and drop the query.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the path argument of uvicorn's access line; never drop the line."""
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):  # noqa: PLR2004
            path, query, _ = args[2].partition("?")
            if query and path.endswith("/callback"):
                record.args = (*args[:2], f"{path}?[hidden]", *args[3:])
        return True


# uvicorn configures its loggers before it imports this module, so the filter stays in place.
logging.getLogger("uvicorn.access").addFilter(HideCallbackQueries())

SESSION_COOKIE = "ca_session"
SIGN_IN_COOKIE = "ca_sign_in"
CSRF_COOKIE = "ca_csrf"
SESSION_MAX_AGE = 7 * 24 * 60 * 60
SIGN_IN_MAX_AGE = 10 * 60
MAX_MESSAGE_CHARACTERS = 4000  # the agent service's limit on one message

# Pages are personal and short-lived: never cached, never framed (no other site can overlay
# an approve button and trick a click), and no referrer, so the sign-in callback's code never
# leaves in a Referer header. Styles come only from /static, so nothing inline is allowed.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; "
        # Forms post here; sign-in and connecting accounts then redirect to these providers.
        "form-action 'self' https://accounts.google.com https://www.dropbox.com "
        "https://trello.com; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",  # frame-ancestors, for browsers that predate it
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

# Cloud Run puts "<tag>---" in front of a revision's tag URL; "---" is reserved for that.
TAG_SEPARATOR = "---"

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


def check_configuration() -> Settings:
    """Read the settings at startup, so a bad one stops the service before it serves."""
    return Settings.from_env()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Read the settings and open one HTTP client to each service for the app's life."""
    settings = check_configuration()
    app.state.settings = settings
    clients = []
    app.state.api = app.state.agent = None
    if settings.api_base_url:
        clients.append(httpx.Client(base_url=settings.api_base_url, timeout=TIMEOUT_SECONDS))
        app.state.api = Api(clients[-1])
    if settings.agent_base_url:
        clients.append(
            httpx.Client(base_url=settings.agent_base_url, timeout=AGENT_TIMEOUT_SECONDS),
        )
        app.state.agent = Agent(clients[-1])
    yield
    for http in clients:
        http.close()


# The interface is pages for people, not an API, so it publishes no OpenAPI schema or docs.
app = FastAPI(
    title="Course Autopilot",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def public_address(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Send a page request on any other address to ``PUBLIC_URL``, the interface's one address.

    Cloud Run serves the interface at two addresses, and cookies belong to one. Google sends
    the browser back to the callback on the public address, so a sign-in started on the other
    would arrive without its binding cookie. A revision's tag URL (the deploy's smoke check)
    and ``/health`` are served where they are.
    """
    public_url = _settings(request).public_url
    host = request.headers.get("host", "")
    if (
        public_url
        and request.method in {"GET", "HEAD"}
        and request.url.path != "/health"
        and host != urlsplit(public_url).netloc
        and TAG_SEPARATOR not in host
    ):
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{public_url}{request.url.path}{query}", status_code=308)
    return await call_next(request)


# Added last, so it runs first: the headers are on every response, redirects included.
@app.middleware("http")
async def security_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Add the security headers to every response."""
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response


class CsrfError(Exception):
    """A form arrived without the CSRF value this browser was given."""


def _settings(request: Request) -> Settings:
    settings: Settings = getattr(request.app.state, "settings", None) or Settings()
    return settings


def _api(request: Request) -> Api | None:
    api: Api | None = getattr(request.app.state, "api", None)
    return api


def _agent(request: Request) -> Agent | None:
    agent: Agent | None = getattr(request.app.state, "agent", None)
    return agent


def _release() -> str:
    return os.environ.get("RELEASE", "unknown")


def _set_cookie(
    request: Request,
    response: Response,
    name: str,
    value: str,
    max_age: int | None,
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=_settings(request).secure_cookies,
        samesite="lax",
        path="/",
    )


def _delete_cookie(request: Request, response: Response, name: str) -> None:
    response.delete_cookie(
        name,
        httponly=True,
        secure=_settings(request).secure_cookies,
        samesite="lax",
        path="/",
    )


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a page with the values every page needs: the release and a CSRF value."""
    csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request,
        template,
        {"release": _release()[:7], "csrf_token": csrf, **(context or {})},
        status_code=status_code,
    )
    if CSRF_COOKIE not in request.cookies:
        _set_cookie(request, response, CSRF_COOKIE, csrf, max_age=None)
    return response


def error_page(  # noqa: PLR0913 (the page, its words and where it points back to)
    request: Request,
    status_code: int,
    title: str,
    next_step: str,
    back: str = "/",
    back_label: str = "Back to Course Autopilot",
) -> HTMLResponse:
    """Render an error with what to do next, and a link back."""
    return render(
        request,
        "error.html",
        {
            "title": title,
            "next_step": next_step,
            "back": back,
            "back_label": back_label,
            "signed_in": SESSION_COOKIE in request.cookies,
        },
        status_code=status_code,
    )


def backend_error_page(request: Request, e: BackendError) -> HTMLResponse:
    """Render an error from the API service as a page that says what to do next."""
    if e.status == 401:  # noqa: PLR2004 (HTTP status)
        response = error_page(request, 401, "You're signed out", "Sign in again to continue.")
        _delete_cookie(request, response, SESSION_COOKIE)
        return response
    if e.code == "sign_in_not_configured":
        return error_page(request, 503, "Sign-in isn't open on this site yet", "Try again later.")
    if e.status >= 500:  # noqa: PLR2004 (HTTP status)
        logger.warning("API service error: %s %s", e.status, e.code)
        return error_page(
            request,
            503,
            "Course Autopilot can't reach its service right now",
            "Nothing was changed. Try again in a minute.",
        )
    return error_page(request, e.status, e.message or "That didn't work", "Go back and try again.")


async def check_csrf(request: Request, csrf_token: Annotated[str, Form()] = "") -> None:
    """Refuse a form that doesn't repeat this browser's CSRF value (threat T8)."""
    expected = request.cookies.get(CSRF_COOKIE, "")
    if not expected or not hmac.compare_digest(expected, csrf_token):
        raise CsrfError


@app.exception_handler(CsrfError)
def _csrf_refused(request: Request, _exc: CsrfError) -> HTMLResponse:
    return error_page(
        request,
        403,
        "This form expired",
        "Nothing was changed. Go back, reload the page and try again.",
    )


CheckedForm = Annotated[None, Depends(check_csrf)]


# The course list reads the person's own Dropbox, so the API answers with one of these until
# Dropbox is connected and usable. For a new user that is the next step, not an error: the
# signed-in page says what to do, keeping the session. Keyed by the API's error code.
CONNECT_PROMPTS = {
    "not_connected": (
        "Connect your Dropbox to see your courses",
        "Course Autopilot keeps each course's syllabus and plan in a folder in your Dropbox.",
    ),
    "reconnect_needed": (
        "Reconnect your Dropbox to see your courses",
        "Dropbox no longer accepts Course Autopilot's access, for example after it was removed "
        "in Dropbox's settings. Nothing in your Dropbox was changed.",
    ),
    "setup_needed": (
        "Finish connecting your accounts",
        "",  # the API's message names the step, e.g. choosing the Trello list
    ),
    "connections_not_configured": (
        "Connecting accounts isn't open on this site yet",
        "Your courses appear here once it is. Try again later.",
    ),
}


@app.get("/", response_class=HTMLResponse)
def start_page(request: Request) -> HTMLResponse:
    """Render the person's courses when signed in; otherwise the start page with sign-in."""
    api, token = _api(request), request.cookies.get(SESSION_COOKIE)
    if api is not None and token:
        try:
            courses = api.courses(token)
        except BackendError as e:
            if e.code in CONNECT_PROMPTS and e.status in {409, 503}:
                title, detail = CONNECT_PROMPTS[e.code]
                prompt = {
                    "title": title,
                    "detail": detail or e.message,
                    # Nothing to connect while connections are off on this service.
                    "link": e.code != "connections_not_configured",
                }
                return render(request, "courses.html", {"prompt": prompt, "signed_in": True})
            if e.status != 401:  # noqa: PLR2004 (HTTP status)
                return backend_error_page(request, e)
            # The session ended (expired, or signed out elsewhere): show the start page.
            response = render(request, "index.html", {"sign_in_open": True})
            _delete_cookie(request, response, SESSION_COOKIE)
            return response
        return render(request, "courses.html", {"courses": courses, "signed_in": True})
    return render(request, "index.html", {"sign_in_open": api is not None})


@app.post("/sign-in", response_model=None)
def sign_in(request: Request, _csrf: CheckedForm) -> Response:
    """Start a Google sign-in and send the browser to Google."""
    api = _api(request)
    if api is None:
        return error_page(request, 503, "Sign-in isn't open on this site yet", "Try again later.")
    try:
        start = api.start_sign_in()
    except BackendError as e:
        return backend_error_page(request, e)
    response = RedirectResponse(start.authorization_url, status_code=303)
    _set_cookie(request, response, SIGN_IN_COOKIE, start.binding, max_age=SIGN_IN_MAX_AGE)
    return response


@app.get("/auth/callback", response_model=None)
def sign_in_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
) -> Response:
    """Finish a Google sign-in: Google sends the browser here with ``code`` and ``state``."""
    api, binding = _api(request), request.cookies.get(SIGN_IN_COOKIE, "")
    if api is None:
        return error_page(request, 503, "Sign-in isn't open on this site yet", "Try again later.")
    if error or not state or not code:
        return error_page(request, 400, "Sign-in was cancelled", "Sign in again when you're ready.")
    if not binding:
        return error_page(
            request,
            400,
            "Sign-in took too long, or started in another browser",
            "Sign in again from this page.",
        )
    try:
        token = api.finish_sign_in(state, code, binding)
    except BackendError as e:
        if e.status == 400:  # noqa: PLR2004 (HTTP status)
            response: Response = error_page(
                request,
                400,
                "Sign-in didn't complete",
                "Sign in again from this page.",
            )
        else:
            response = backend_error_page(request, e)
        _delete_cookie(request, response, SIGN_IN_COOKIE)
        return response
    response = RedirectResponse("/", status_code=303)
    _set_cookie(request, response, SESSION_COOKIE, token, max_age=SESSION_MAX_AGE)
    _delete_cookie(request, response, SIGN_IN_COOKIE)
    return response


@app.post("/sign-out", response_model=None)
def sign_out(request: Request, _csrf: CheckedForm) -> Response:
    """End the session at the API service and forget it in this browser."""
    api, token = _api(request), request.cookies.get(SESSION_COOKIE)
    if api is not None and token:
        try:
            api.sign_out(token)
        except BackendError as e:
            # The cookie goes either way; an unreachable API means the session simply
            # expires on its own (24 hours idle).
            logger.warning("Sign-out at the API service failed: %s %s", e.status, e.code)
    response = RedirectResponse("/", status_code=303)
    _delete_cookie(request, response, SESSION_COOKIE)
    return response


# ----- Connections: the person's own Dropbox, Google Calendar and Trello (#95; API #98-#101) -----

PROVIDERS = {"dropbox": "Dropbox", "google_calendar": "Google Calendar", "trello": "Trello"}
# What each account holds, in the person's terms (DESIGN.md, the three verticals).
USES = {
    "dropbox": "Your syllabi and course plans, in an app folder",
    "google_calendar": "Deadlines, as events",
    "trello": "Tasks, as cards in a list you choose",
}
BACK_TO_CONNECTIONS = {"back": "/connections", "back_label": "Back to your connections"}


def _session(request: Request) -> tuple[Api, str] | Response:
    """Return the API and the session token, or the page to show instead."""
    api, token = _api(request), request.cookies.get(SESSION_COOKIE)
    if api is None:
        return error_page(
            request,
            503,
            "Connecting accounts isn't open on this site yet",
            "Try again later.",
        )
    if not token:
        return RedirectResponse("/", status_code=303)  # sign in first
    return api, token


def _unknown_provider(request: Request) -> HTMLResponse:
    return error_page(
        request,
        404,
        "There's no such account to connect",
        "Choose one from your connections.",
        **BACK_TO_CONNECTIONS,
    )


def _connection_error(request: Request, e: BackendError, title: str) -> HTMLResponse:
    """Render a refused or failed connection step, pointing back to the connections page."""
    if e.code == "connection_not_configured":  # this one provider isn't set up on the API
        return error_page(
            request,
            503,
            f"Connecting {title} isn't available on this site yet",
            "Try again later.",
            **BACK_TO_CONNECTIONS,
        )
    if e.status == 401 or e.status >= 500:  # noqa: PLR2004 (HTTP status)
        return backend_error_page(request, e)
    if e.code == "connect_failed":
        return error_page(
            request,
            400,
            f"{title} didn't connect",
            "Nothing was saved. Connect it again from your connections.",
            **BACK_TO_CONNECTIONS,
        )
    return error_page(
        request,
        e.status,
        e.message or f"{title} didn't connect",
        "Go back to your connections and try again.",
        **BACK_TO_CONNECTIONS,
    )


@app.get("/connections", response_model=None)
def connections_page(request: Request) -> Response:
    """Show each provider: connected or not, and what to do next."""
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        connections = api.connections(token)
    except BackendError as e:
        if e.code == "connections_not_configured":
            return error_page(
                request,
                503,
                "Connecting accounts isn't open on this site yet",
                "Try again later.",
            )
        return backend_error_page(request, e)
    return render(
        request,
        "connections.html",
        {"connections": connections, "uses": USES, "signed_in": True},
    )


@app.post("/connections/{provider}/connect", response_model=None)
def connect(request: Request, provider: str, _csrf: CheckedForm) -> Response:
    """Start connecting a provider and send the browser to it."""
    if provider not in PROVIDERS:
        return _unknown_provider(request)
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        url = api.start_connection(token, provider)
    except BackendError as e:
        return _connection_error(request, e, PROVIDERS[provider])
    return RedirectResponse(url, status_code=303)


def _finish(request: Request, provider: str, state: str, code: str) -> Response:
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        connection = api.finish_connection(token, provider, state, code)
    except BackendError as e:
        return _connection_error(request, e, PROVIDERS[provider])
    # Trello isn't usable until the person picks the list their cards go to.
    target = "/connections/trello/list" if connection.status == "setup_needed" else "/connections"
    return RedirectResponse(target, status_code=303)


@app.get("/connections/{provider}/callback", response_model=None)
def connection_callback(
    request: Request,
    provider: str,
    state: str = "",
    code: str = "",
    error: str = "",
) -> Response:
    """Where each provider sends the browser back.

    Dropbox and Google put ``state`` and ``code`` in the query. Trello puts its token in the
    URL fragment, which never reaches a server, so its page hands the token to a script that
    posts it back (``static/trello-return.js``). The API ties each attempt to the session that
    started it and uses it once, so a callback finished in another session fails there (T9).
    """
    if provider not in PROVIDERS:
        return _unknown_provider(request)
    title = PROVIDERS[provider]
    if error or not state:
        return error_page(
            request,
            400,
            f"{title} wasn't connected",
            "Nothing was saved. Connect it again when you're ready.",
            **BACK_TO_CONNECTIONS,
        )
    if provider == "trello":
        signed_in = SESSION_COOKIE in request.cookies
        return render(request, "trello_return.html", {"state": state, "signed_in": signed_in})
    if not code:
        return error_page(
            request,
            400,
            f"{title} wasn't connected",
            "Nothing was saved. Connect it again when you're ready.",
            **BACK_TO_CONNECTIONS,
        )
    return _finish(request, provider, state, code)


@app.post("/connections/trello/finish", response_model=None)
def finish_trello(
    request: Request,
    _csrf: CheckedForm,
    state: Annotated[str, Form()] = "",
    token: Annotated[str, Form()] = "",
) -> Response:
    """Finish connecting Trello with the token its return page posted."""
    if not state or not token:
        return error_page(
            request,
            400,
            "Trello wasn't connected",
            "Nothing was saved. Connect it again when you're ready.",
            **BACK_TO_CONNECTIONS,
        )
    return _finish(request, "trello", state, token)


@app.get("/connections/trello/list", response_model=None)
def trello_list_page(request: Request) -> Response:
    """Let the person choose the Trello list their cards go to."""
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        lists = api.trello_lists(token)
    except BackendError as e:
        return _connection_error(request, e, "Trello")
    boards: dict[str, list[TrelloList]] = {}
    for item in lists:
        boards.setdefault(item.board, []).append(item)
    return render(request, "trello_list.html", {"boards": boards, "signed_in": True})


@app.post("/connections/trello/list", response_model=None)
def choose_trello_list(
    request: Request,
    _csrf: CheckedForm,
    list_id: Annotated[str, Form()] = "",
) -> Response:
    """Save the chosen Trello list."""
    if not list_id:
        return error_page(
            request,
            400,
            "Choose a list first",
            "Pick the list your cards should go to.",
            back="/connections/trello/list",
            back_label="Back to your Trello lists",
        )
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        api.choose_trello_list(token, list_id)
    except BackendError as e:
        return _connection_error(request, e, "Trello")
    return RedirectResponse("/connections", status_code=303)


@app.post("/connections/{provider}/disconnect", response_model=None)
def disconnect(request: Request, provider: str, _csrf: CheckedForm) -> Response:
    """Forget the connection here and revoke it at the provider."""
    if provider not in PROVIDERS:
        return _unknown_provider(request)
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        api.disconnect(token, provider)
    except BackendError as e:
        return _connection_error(request, e, PROVIDERS[provider])
    return RedirectResponse("/connections", status_code=303)


# ----- Conversations and proposals (#95; agent #94, proposals #89 and #90) -----
#
# A turn goes to the agent service with an agent token made for that one turn: it can read
# and propose, never approve or execute (identity.py, T14). Approving, rejecting, executing
# and settling go to the API with the person's own session, from forms on our pages only.

# Shown after a redirect, chosen by key: no text from the URL ever reaches the page.
NOTICES = {
    "kept": "Here are the changes you kept. Nothing has been approved yet: review them and "
    "approve.",
    "paused": "Making changes is paused on this site right now. Your approval is kept: "
    "try again later.",
    "none_kept": "You unchecked every change, so the proposal was rejected. Nothing was changed.",
    "settled": "Thanks. That change is recorded.",
    "kept_old_open": "Here are the changes you kept. Nothing has been approved yet. The "
    "earlier proposal couldn't be rejected, so it's still open: approve only this one.",
    "running": "The changes are still being made. Wait a few minutes, reload this page, and "
    "run them again if it hasn't finished.",
}

# A turn the agent service refused: what the person sees above the message box.
TURN_ERRORS = {
    "assistant_busy": "The assistant is busy right now. Try again in a minute.",
    "daily_limit_reached": "You've reached today's limit for the assistant. Your course "
    "plans and approvals still work.",
    "not_connected": "Connect your accounts first: the assistant reads your Dropbox.",
    "reconnect_needed": "Reconnect your accounts: Dropbox no longer accepts Course "
    "Autopilot's access.",
}
UNAVAILABLE = "The assistant isn't available right now. Your courses and approvals still work."


def _turn_refused(e: BackendError) -> tuple[str, int]:
    """Return what to tell the person about a turn the agent side refused, and the status."""
    if e.status == 401:  # noqa: PLR2004 (HTTP status)
        # The agent side refused the turn's token, not the session, which the API had just
        # accepted (clock skew, a misconfiguration): the person stays signed in.
        return UNAVAILABLE, 503
    message = TURN_ERRORS.get(e.code) or (UNAVAILABLE if e.status >= 500 else e.message)  # noqa: PLR2004
    return message or UNAVAILABLE, e.status


def _valid_id(value: str) -> bool:
    """Whether ``value`` is a UUID, so it can go into an API path as one segment."""
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _not_found(request: Request, what: str) -> HTMLResponse:
    return error_page(request, 404, f"There's no such {what}", "Go back to your courses.")


def _chat_page(  # noqa: PLR0913 (the page and what it shows)
    request: Request,
    conversation_id: str,
    entries: list[ChatEntry],
    notice: str = "",
    draft: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "chat.html",
        {
            "conversation_id": conversation_id,
            "entries": entries,
            "notice": notice,
            "draft": draft,
            "assistant_open": _agent(request) is not None,
            "max_length": MAX_MESSAGE_CHARACTERS,
            "signed_in": True,
        },
        status_code=status_code,
    )


@app.post("/chat", response_model=None)
def new_conversation(request: Request, _csrf: CheckedForm) -> Response:
    """Start a conversation with the assistant."""
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        conversation_id = api.create_conversation(token)
    except BackendError as e:
        return backend_error_page(request, e)
    return RedirectResponse(f"/chat/{conversation_id}", status_code=303)


@app.get("/chat/{conversation_id}", response_model=None)
def conversation_page(request: Request, conversation_id: str) -> Response:
    """Show a conversation and a box for the next message."""
    if not _valid_id(conversation_id):
        return _not_found(request, "conversation")
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        entries = api.conversation(token, conversation_id)
    except BackendError as e:
        if e.status == 404:  # noqa: PLR2004 (HTTP status)
            return _not_found(request, "conversation")
        return backend_error_page(request, e)
    return _chat_page(request, conversation_id, entries)


@app.post("/chat/{conversation_id}", response_model=None)
def send_message(  # noqa: PLR0911 (each way a turn can end)
    request: Request,
    conversation_id: str,
    _csrf: CheckedForm,
    text: Annotated[str, Form()] = "",
) -> Response:
    """Run one turn with what the person wrote, then show the conversation again."""
    if not _valid_id(conversation_id):
        return _not_found(request, "conversation")
    session = _session(request)
    if isinstance(session, Response):
        return session
    api, token = session
    agent, text = _agent(request), text.strip()

    def again(notice: str, status_code: int) -> Response:
        try:
            entries = api.conversation(token, conversation_id)
        except BackendError as e:
            return backend_error_page(request, e)
        return _chat_page(request, conversation_id, entries, notice, text, status_code)

    if agent is None:
        return again("The assistant isn't open on this site yet.", 503)
    if not text or len(text) > MAX_MESSAGE_CHARACTERS:
        return again(f"Write a message of 1 to {MAX_MESSAGE_CHARACTERS} characters.", 400)
    try:
        # A fresh agent token for this turn: it ends with the session, and in 5 minutes.
        agent_token = api.agent_token(token)
    except BackendError as e:
        return backend_error_page(request, e)  # the API refused the session itself: sign in
    try:
        agent.take_turn(agent_token, conversation_id, text)
    except BackendError as e:
        if e.status == 404:  # noqa: PLR2004 (HTTP status)
            return _not_found(request, "conversation")
        return again(*_turn_refused(e))
    # The reply is stored with the conversation; showing it from there keeps reloads safe.
    return RedirectResponse(f"/chat/{conversation_id}", status_code=303)


def _proposal_url(proposal_id: str, notice: str = "", conversation: str = "") -> str:
    """Return a proposal page's address, keeping the conversation it came from."""
    query = {k: v for k, v in (("notice", notice), ("conversation", conversation)) if v}
    return f"/proposals/{proposal_id}" + (f"?{urlencode(query)}" if query else "")


def _proposal_page(
    request: Request,
    proposal: Proposal,
    notice: str = "",
    conversation: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "proposal.html",
        {
            "proposal": proposal,
            "changes": change_views(proposal.changes, proposal.outcomes),
            "status_text": STATUSES.get(proposal.status, proposal.status),
            "notice": notice,
            "conversation": conversation,
            "signed_in": True,
        },
        status_code=status_code,
    )


def _proposal_error(request: Request, e: BackendError, proposal_id: str) -> Response:
    """Render a refused proposal step: what to do next, pointing back to the proposal."""
    if e.status == 401 or (e.status >= 500 and e.code != "execution_disabled"):  # noqa: PLR2004
        return backend_error_page(request, e)
    if e.status == 404:  # noqa: PLR2004 (HTTP status)
        return _not_found(request, "proposal")
    if e.code == "execution_disabled":
        return RedirectResponse(f"/proposals/{proposal_id}?notice=paused", status_code=303)
    return error_page(
        request,
        e.status,
        e.message or "That didn't work",
        "Nothing else was changed. Go back to the proposal, reload it and try again.",
        back=f"/proposals/{proposal_id}",
        back_label="Back to the proposal",
    )


def _proposal_session(request: Request, proposal_id: str) -> tuple[Api, str] | Response:
    if not _valid_id(proposal_id):
        return _not_found(request, "proposal")
    return _session(request)


@app.get("/proposals/{proposal_id}", response_model=None)
def proposal_page(
    request: Request,
    proposal_id: str,
    notice: str = "",
    conversation: str = "",
) -> Response:
    """Show every proposed change exactly as stored, its outcome, and what to do next.

    ``conversation`` is the chat the person came from (the API's proposal doesn't name it): it
    links back there, and a proposal made from kept changes joins the same conversation.
    """
    session = _proposal_session(request, proposal_id)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        proposal = api.proposal(token, proposal_id)
    except BackendError as e:
        return _proposal_error(request, e, proposal_id)
    conversation = conversation if _valid_id(conversation) else ""
    return _proposal_page(request, proposal, NOTICES.get(notice, ""), conversation)


@app.post("/proposals/{proposal_id}/approve", response_model=None)
def approve(  # noqa: PLR0911 (each outcome of approving)
    request: Request,
    proposal_id: str,
    _csrf: CheckedForm,
    changes_hash: Annotated[str, Form()] = "",
    keep: Annotated[list[str] | None, Form()] = None,
    conversation_id: Annotated[str, Form()] = "",
) -> Response:
    """Approve exactly the changes the page showed, and make them; or drop unchecked ones.

    If every change is still checked, the proposal is approved with the hash of the content
    the page showed (the API refuses any other) and then run. If some were unchecked,
    nothing is approved: this proposal is rejected and a new one with the kept changes is
    shown for review, so the person approves exactly what will happen (S1, step 6). The new
    one is made before this one is rejected, so a failure leaves the person's proposal as it was.
    """
    session = _proposal_session(request, proposal_id)
    if isinstance(session, Response):
        return session
    api, token = session
    if not all(k.isdigit() for k in keep or []):
        return error_page(
            request,
            400,
            "That form wasn't one of ours",
            "Nothing was approved. Go back to the proposal and try again.",
            back=f"/proposals/{proposal_id}",
            back_label="Back to the proposal",
        )
    conversation = conversation_id if _valid_id(conversation_id) else ""
    try:
        proposal = api.proposal(token, proposal_id)
        if proposal.changes_hash != changes_hash:
            return error_page(
                request,
                409,
                "These changes are different from the ones you reviewed",
                "Nothing was approved. Go back, review the proposal again, and approve it.",
                back=f"/proposals/{proposal_id}",
                back_label="Back to the proposal",
            )
        kept = sorted({int(k) for k in keep or [] if int(k) < len(proposal.changes)})
        if not kept:
            api.reject(token, proposal_id)
            return RedirectResponse(
                _proposal_url(proposal_id, "none_kept", conversation),
                status_code=303,
            )
        if len(kept) < len(proposal.changes):
            changes = [proposal.changes[i] for i in kept]
            new = api.create_proposal(token, changes, conversation or None)
            notice = "kept"
            try:
                api.reject(token, proposal_id)
            except BackendError:
                notice = "kept_old_open"  # the new one exists: show it, and say so
            return RedirectResponse(_proposal_url(new.id, notice, conversation), status_code=303)
        api.approve(token, proposal_id, changes_hash)
        api.execute(token, proposal_id)
    except BackendError as e:
        return _proposal_error(request, e, proposal_id)
    return RedirectResponse(_proposal_url(proposal_id, conversation=conversation), status_code=303)


@app.post("/proposals/{proposal_id}/reject", response_model=None)
def reject(request: Request, proposal_id: str, _csrf: CheckedForm) -> Response:
    """Reject the whole proposal: nothing is changed."""
    session = _proposal_session(request, proposal_id)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        api.reject(token, proposal_id)
    except BackendError as e:
        return _proposal_error(request, e, proposal_id)
    return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)


def _still_running(api: Api, token: str, proposal_id: str) -> bool:
    try:
        return api.proposal(token, proposal_id).status == "executing"
    except BackendError:
        return False


@app.post("/proposals/{proposal_id}/execute", response_model=None)
def execute(request: Request, proposal_id: str, _csrf: CheckedForm) -> Response:
    """Make an approved proposal's changes, retry failed ones, or take over a stopped run.

    A proposal left ``executing`` by a run that stopped can run again once the API sees the run
    has been silent long enough; until then the API answers 409, and the page says to wait.
    """
    session = _proposal_session(request, proposal_id)
    if isinstance(session, Response):
        return session
    api, token = session
    try:
        api.execute(token, proposal_id)
    except BackendError as e:
        if e.status == 409 and _still_running(api, token, proposal_id):  # noqa: PLR2004
            return RedirectResponse(_proposal_url(proposal_id, "running"), status_code=303)
        return _proposal_error(request, e, proposal_id)
    return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)


@app.post("/proposals/{proposal_id}/changes/{change_index}/resolve", response_model=None)
def resolve(
    request: Request,
    proposal_id: str,
    change_index: int,
    _csrf: CheckedForm,
    happened: Annotated[str, Form()] = "",
    record_id: Annotated[str, Form()] = "",
) -> Response:
    """Record what the person found when they checked an uncertain change."""
    session = _proposal_session(request, proposal_id)
    if isinstance(session, Response):
        return session
    api, token = session
    if happened not in {"yes", "no"}:
        return error_page(
            request,
            400,
            "Say whether the change happened",
            "Go back and choose one.",
            back=f"/proposals/{proposal_id}",
            back_label="Back to the proposal",
        )
    status = "completed" if happened == "yes" else "failed"
    try:
        api.resolve(token, proposal_id, change_index, status, record_id.strip() or None)
    except BackendError as e:
        return _proposal_error(request, e, proposal_id)
    return RedirectResponse(f"/proposals/{proposal_id}?notice=settled", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: answers without calling the API service."""
    return {"status": "ok", "release": _release()}
