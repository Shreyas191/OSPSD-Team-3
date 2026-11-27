"""Google sign-in (OpenID Connect, authorization code with PKCE) for Course Autopilot (#96).

The interface drives the browser; the API service holds every secret and makes the session:

1. ``start`` returns Google's authorization URL and a random ``binding``. The interface keeps
   the binding in an ``HttpOnly`` cookie and sends the browser to the URL. A
   ``sign_in_attempts`` row keeps the hashes of ``state`` and the binding, the PKCE verifier
   and the nonce.
2. Google sends the browser back to the interface with ``code`` and ``state``. The interface
   passes them, with the binding from its cookie, to ``finish``.
3. ``finish`` uses the attempt once (a second use fails even if the first didn't finish),
   checks that the binding matches (so a callback carried to another browser fails, threat
   T9), exchanges the code with the verifier, and checks the ID token: Google's signature,
   our client ID as audience, the issuer, expiry, and our nonce. The account is found or
   created by Google's stable subject ID, never by email (DESIGN.md P5).

Any failure is one message, "Sign-in didn't complete; try again", so the response doesn't say
which check failed. Google being unreachable is 503.
"""

import base64
import hashlib
import hmac
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from urllib.parse import urlencode

import httpx
from course_autopilot_store.models import SignInAttempt, User
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from cloud_storage_service.identity import hash_token

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a password)
ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
SCOPES = "openid email profile"
ATTEMPT_LIFETIME = timedelta(minutes=10)
GOOGLE_AUTH_PROVIDER = "google"

CLIENT_ID_ENV_VAR = "GOOGLE_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV_VAR = "GOOGLE_OAUTH_CLIENT_SECRET"  # noqa: S105 (a variable name)
REDIRECT_URI_ENV_VAR = "SIGN_IN_REDIRECT_URI"

FAILED = "Sign-in didn't complete; try again"

# Verifies an ID token for an audience and returns its claims; raises ValueError if invalid.
TokenVerifier = Callable[[str, str], Mapping[str, Any]]


class SignInError(Exception):
    """Sign-in failed. ``status`` is 400 for anything about the attempt, 503 for Google."""

    def __init__(self, status: int, code: str, message: str = FAILED) -> None:
        """Record the HTTP status, the shared error code and the message."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GoogleSettings:
    """Our OAuth client at Google and where Google sends the browser back."""

    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls) -> Self | None:
        """Read the three settings; ``None`` (sign-in off) unless all three are set."""
        values = [
            os.environ.get(name, "")
            for name in (CLIENT_ID_ENV_VAR, CLIENT_SECRET_ENV_VAR, REDIRECT_URI_ENV_VAR)
        ]
        return cls(*values) if all(values) else None


def verify_with_google(token: str, audience: str) -> Mapping[str, Any]:
    """Check an ID token's signature against Google's keys, its audience and expiry."""
    from google.auth.transport import requests as google_requests  # noqa: PLC0415
    from google.oauth2 import id_token  # noqa: PLC0415

    claims: Mapping[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
        token,
        google_requests.Request(),
        audience,
    )
    return claims


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(frozen=True)
class SignedIn:
    """Who signed in: our user, and the name to show."""

    user_id: Any
    display_name: str


class GoogleSignIn:
    """Starts and finishes Google sign-ins with one OAuth client."""

    def __init__(
        self,
        settings: GoogleSettings,
        http: httpx.Client,
        verify: TokenVerifier = verify_with_google,
    ) -> None:
        """Bind the client settings, an HTTP client for Google, and the ID token check."""
        self._settings = settings
        self._http = http
        self._verify = verify

    def start(self, session: Session) -> tuple[str, str]:
        """Record a new attempt; return Google's authorization URL and the browser binding."""
        state, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, nonce = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        # Attempts never finished are removed once they can no longer be used.
        session.execute(
            delete(SignInAttempt).where(SignInAttempt.created_at < now - ATTEMPT_LIFETIME),
        )
        session.add(
            SignInAttempt(
                state_hash=hash_token(state),
                binding_hash=hash_token(binding),
                code_verifier=verifier,
                nonce=nonce,
                created_at=now,
            ),
        )
        query = urlencode(
            {
                "client_id": self._settings.client_id,
                "redirect_uri": self._settings.redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "state": state,
                "nonce": nonce,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "prompt": "select_account",
            },
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}", binding

    @staticmethod
    def use_attempt(session: Session, state: str, binding: str) -> tuple[str, str] | None:
        """Use the attempt for ``state`` once; return its PKCE verifier and nonce, or None.

        It returns instead of raising, so its transaction commits and the attempt stays used
        whatever the outcome: a failed check can't be retried with the same ``state``.
        """
        now = datetime.now(UTC)
        attempt = session.scalars(
            select(SignInAttempt).where(SignInAttempt.state_hash == hash_token(state)),
        ).first()
        if attempt is None:
            return None
        created = attempt.created_at
        created = created if created.tzinfo else created.replace(tzinfo=UTC)
        # Only one request can change used_at from empty, even when two arrive together.
        used = session.execute(
            update(SignInAttempt)
            .where(SignInAttempt.id == attempt.id, SignInAttempt.used_at.is_(None))
            .values(used_at=now),
        )
        if (
            used.rowcount != 1  # type: ignore[attr-defined]  # a CursorResult for an UPDATE
            or now - created > ATTEMPT_LIFETIME
            or not hmac.compare_digest(attempt.binding_hash, hash_token(binding))
        ):
            return None
        return attempt.code_verifier, attempt.nonce

    def claims_for(self, code: str, verifier: str, nonce: str) -> Mapping[str, Any]:
        """Exchange the code at Google and return the ID token's checked claims."""
        try:
            response = self._http.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                    "redirect_uri": self._settings.redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
        except httpx.HTTPError:
            message = "Google can't be reached; try again"
            raise SignInError(503, "google_unavailable", message) from None
        if response.status_code != httpx.codes.OK:
            raise SignInError(400, "sign_in_failed")
        token = response.json().get("id_token")
        try:
            claims = self._verify(token, self._settings.client_id) if token else {}
        except ValueError:
            claims = {}
        if (
            claims.get("iss") not in ISSUERS
            or not claims.get("sub")
            or not hmac.compare_digest(str(claims.get("nonce", "")), nonce)
        ):
            raise SignInError(400, "sign_in_failed")
        return claims

    @staticmethod
    def account_for(session: Session, claims: Mapping[str, Any]) -> SignedIn:
        """Find or create the user for Google's subject ID; keep their name current."""
        subject = str(claims["sub"])
        name = str(claims.get("name") or claims.get("email") or "")[:200]
        user = session.scalars(
            select(User).where(
                User.auth_provider == GOOGLE_AUTH_PROVIDER,
                User.auth_subject == subject,
            ),
        ).first()
        if user is None:
            user = User(auth_provider=GOOGLE_AUTH_PROVIDER, auth_subject=subject)
            session.add(user)
        user.display_name = name
        session.flush()
        return SignedIn(user.id, name)
