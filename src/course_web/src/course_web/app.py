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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from course_web.backend import TIMEOUT_SECONDS, Api, BackendError
from course_web.config import Settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "ca_session"
SIGN_IN_COOKIE = "ca_sign_in"
CSRF_COOKIE = "ca_csrf"
SESSION_MAX_AGE = 7 * 24 * 60 * 60
SIGN_IN_MAX_AGE = 10 * 60

# Pages are personal and short-lived: never cached, never framed (no other site can overlay
# an approve button and trick a click), and no referrer, so the sign-in callback's code never
# leaves in a Referer header. Styles come only from /static, so nothing inline is allowed.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; "
        "form-action 'self' https://accounts.google.com; frame-ancestors 'none'"
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
    """Read the settings and open one HTTP client to the API service for the app's life."""
    settings = check_configuration()
    app.state.settings = settings
    http = (
        httpx.Client(base_url=settings.api_base_url, timeout=TIMEOUT_SECONDS)
        if settings.api_base_url
        else None
    )
    app.state.api = Api(http) if http else None
    yield
    if http is not None:
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


def error_page(request: Request, status_code: int, title: str, next_step: str) -> HTMLResponse:
    """Render an error with what to do next."""
    return render(
        request,
        "error.html",
        {"title": title, "next_step": next_step, "signed_in": SESSION_COOKIE in request.cookies},
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
                prompt = {"title": title, "detail": detail or e.message}
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


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: answers without calling the API service."""
    return {"status": "ok", "release": _release()}
