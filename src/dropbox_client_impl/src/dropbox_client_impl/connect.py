"""Connecting a person's own Dropbox to Course Autopilot (#99).

The Course Autopilot API service runs this OAuth flow for each person, with PKCE. It uses
the service's Dropbox app (``DROPBOX_APP_KEY`` / ``DROPBOX_APP_SECRET``), which has **App
folder** access, so a person's token can only reach ``/Apps/<our app>/`` in their Dropbox
(DESIGN.md T11). The redirect URI is ``DROPBOX_CONNECT_REDIRECT_URI``. All three are read in
:mod:`dropbox_client_impl.auth`, like every other Dropbox credential.

- :func:`authorization_url`: where to send the browser, for a ``state`` and PKCE challenge.
- :func:`exchange_code`: the code and verifier for a refresh token and the account's email.
- :func:`client_for`: a ``DropboxClient`` acting as that person, from their refresh token.
- :func:`revoke`: ends the refresh token and every access token made from it.
- :func:`is_auth_failure`: whether an error came from Dropbox refusing the token.

No function here logs or returns a token in an error message.
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import dropbox
import requests
from dropbox.exceptions import AuthError, DropboxException

from dropbox_client_impl.auth import (
    REQUEST_TIMEOUT_SECONDS,
    ConnectSettings,
    DropboxAuthError,
    DropboxConnectionError,
)
from dropbox_client_impl.dropbox_impl import DropboxClient

AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"  # noqa: S105 (a URL)


@dataclass(frozen=True)
class DropboxGrant:
    """What a finished authorization gives: a refresh token, and whose account it is."""

    refresh_token: str
    account_id: str
    email: str
    scopes: str


def authorization_url(settings: ConnectSettings, state: str, code_challenge: str) -> str:
    """Return Dropbox's authorization URL for an offline (refresh) token, with PKCE (S256)."""
    query = urlencode(
        {
            "client_id": settings.app_key,
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
            "token_access_type": "offline",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
    )
    return f"{AUTHORIZE_URL}?{query}"


def _sdk(settings: ConnectSettings, refresh_token: str) -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=settings.app_key,
        app_secret=settings.app_secret,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def exchange_code(settings: ConnectSettings, code: str, code_verifier: str) -> DropboxGrant:
    """Exchange an authorization code for a refresh token, and read the account's email.

    Raises:
        DropboxAuthError: If Dropbox refuses the code (wrong, used, or expired), or sends no
            refresh token.
        DropboxConnectionError: If Dropbox can't be reached.

    """
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.redirect_uri,
                # With PKCE the verifier replaces the app secret, as in the SDK's own flow
                # (DropboxOAuth2FlowBase._finish sends one or the other, never both).
                "code_verifier": code_verifier,
                "client_id": settings.app_key,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        msg = "Could not reach Dropbox to finish connecting"
        raise DropboxConnectionError(msg) from e
    body = response.json() if response.ok else {}
    refresh_token = str(body.get("refresh_token") or "")
    if not refresh_token:
        msg = "Dropbox didn't accept the authorization"
        raise DropboxAuthError(msg)
    email = ""
    try:
        email = str(_sdk(settings, refresh_token).users_get_current_account().email)
    except (DropboxException, requests.RequestException):
        email = ""  # The connection still works; the page just shows no address.
    return DropboxGrant(
        refresh_token=refresh_token,
        account_id=str(body.get("account_id") or ""),
        email=email,
        scopes=str(body.get("scope") or ""),
    )


def client_for(settings: ConnectSettings, refresh_token: str) -> DropboxClient:
    """Return a client acting as the person who granted ``refresh_token`` (no network call)."""
    return DropboxClient(dbx=_sdk(settings, refresh_token))


def revoke(settings: ConnectSettings, refresh_token: str) -> None:
    """Revoke the refresh token at Dropbox, with every access token made from it.

    Raises:
        DropboxConnectionError: If Dropbox can't be reached or refuses (the token may stay
            valid until the person removes the app in their Dropbox settings).

    """
    try:
        _sdk(settings, refresh_token).auth_token_revoke()
    except AuthError:
        return  # Already revoked or expired: nothing left to end.
    except (DropboxException, requests.RequestException) as e:
        msg = "Could not revoke the Dropbox token"
        raise DropboxConnectionError(msg) from e


def is_auth_failure(error: BaseException) -> bool:
    """Whether ``error`` (or one it was raised from) is Dropbox refusing the token."""
    seen: BaseException | None = error
    for _ in range(10):
        if seen is None:
            return False
        if isinstance(seen, AuthError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False
