"""Sign-in, sessions and agent tokens over HTTP (#96, #97).

- ``POST /auth/google/start`` (anyone): a new attempt; Google's URL and the browser binding.
- ``POST /auth/google/finish`` (anyone, with the binding): checks the attempt and Google's
  answer, and returns a new session.
- ``GET /auth/me`` (any token): who is signed in, and with which kind of token.
- ``POST /auth/agent-token`` (the person's session): a 5-minute read-and-propose token for
  one agent turn.
- ``POST /auth/sign-out`` (the person's session): ends this session and its agent tokens.
- ``POST /auth/sign-out-everywhere`` (the person's session): ends every session of the user.

Tokens are returned once, in the body, and only their hashes are stored. The interface keeps
the session in a ``Secure``, ``HttpOnly`` cookie and sends it as ``Authorization: Bearer``.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cloud_storage_service.errors import ErrorResponse, SharedErrorRoute, error_response
from cloud_storage_service.identity import (
    AnyCaller,
    Person,
    RequiredDatabase,
    create_agent_token,
    create_session,
    end_every_session,
    end_session,
)
from cloud_storage_service.sign_in import GoogleSignIn, SignInError

router = APIRouter(prefix="/auth", tags=["sign-in"], route_class=SharedErrorRoute)


class SignInNotConfiguredError(Exception):
    """No Google OAuth client is configured on this service."""


def get_google_sign_in(request: Request) -> GoogleSignIn:
    """Return the service's Google sign-in, or answer 503 when it isn't configured."""
    google: GoogleSignIn | None = getattr(request.app.state, "google_sign_in", None)
    if google is None:
        raise SignInNotConfiguredError
    return google


Google = Annotated[GoogleSignIn, Depends(get_google_sign_in)]

_TOKEN = Field(min_length=1, max_length=2048)


class StartResponse(BaseModel):
    """Where to send the browser, and the value to keep in its cookie until it returns."""

    authorization_url: str
    binding: str


class FinishRequest(BaseModel):
    """What Google sent back to the interface, and the binding from the browser's cookie."""

    model_config = ConfigDict(extra="forbid")

    state: str = _TOKEN
    code: str = _TOKEN
    binding: str = _TOKEN


class UserView(BaseModel):
    """A signed-in user, as the interface shows them."""

    id: uuid.UUID
    display_name: str


class SessionResponse(BaseModel):
    """A new session. ``session_token`` is shown only here."""

    session_token: str
    expires_at: datetime
    user: UserView


class AgentTokenResponse(BaseModel):
    """A token for one agent turn: read and propose only."""

    token: str
    expires_at: datetime


class MeResponse(BaseModel):
    """Who is calling, and whether with their session or an agent token."""

    user_id: uuid.UUID
    scope: str


ERRORS: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "Sign-in didn't complete"},
    401: {"model": ErrorResponse, "description": "Not signed in"},
    403: {"model": ErrorResponse, "description": "An agent token can't do this"},
    503: {"model": ErrorResponse, "description": "Sign-in isn't configured, or Google is down"},
}


def _sign_in_error(e: SignInError) -> JSONResponse:
    return error_response(e.status, e.code, e.message)


@router.post("/google/start", responses=ERRORS)
def start(google: Google, database: RequiredDatabase) -> StartResponse:
    """Begin a Google sign-in."""
    with database.session() as session:
        url, binding = google.start(session)
    return StartResponse(authorization_url=url, binding=binding)


@router.post("/google/finish", responses=ERRORS, response_model=SessionResponse)
def finish(
    body: FinishRequest,
    google: Google,
    database: RequiredDatabase,
) -> SessionResponse | JSONResponse:
    """Finish a Google sign-in and start a session."""
    with database.session() as session:
        attempt = google.use_attempt(session, body.state, body.binding)
    if attempt is None:
        return _sign_in_error(SignInError(status.HTTP_400_BAD_REQUEST, "sign_in_failed"))
    try:
        claims = google.claims_for(body.code, *attempt)
    except SignInError as e:
        return _sign_in_error(e)
    with database.session() as session:
        signed_in = google.account_for(session, claims)
        token, expires = create_session(session, signed_in.user_id)
    return SessionResponse(
        session_token=token,
        expires_at=expires,
        user=UserView(id=signed_in.user_id, display_name=signed_in.display_name),
    )


@router.get("/me", responses=ERRORS)
def me(caller: AnyCaller) -> MeResponse:
    """Return who is signed in."""
    return MeResponse(user_id=caller.user_id, scope=caller.scope)


@router.post("/agent-token", responses=ERRORS)
def agent_token(person: Person, database: RequiredDatabase) -> AgentTokenResponse:
    """Make a read-and-propose token for one agent turn."""
    with database.session() as session:
        token, expires = create_agent_token(session, person)
    return AgentTokenResponse(token=token, expires_at=expires)


@router.post("/sign-out", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
def sign_out(person: Person, database: RequiredDatabase) -> Response:
    """End this session and the agent tokens made from it."""
    with database.session() as session:
        end_session(session, person)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sign-out-everywhere", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
def sign_out_everywhere(person: Person, database: RequiredDatabase) -> Response:
    """End every session and agent token of this user."""
    with database.session() as session:
        end_every_session(session, person.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
