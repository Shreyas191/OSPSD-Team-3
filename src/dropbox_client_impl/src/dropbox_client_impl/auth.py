"""Dropbox authentication and authenticated HTTP requests.

This is the only module that knows where the Dropbox access token comes from.
For now it is a manually generated token read from ``DROPBOX_ACCESS_TOKEN``
(environment variable or local ``.env`` file). CRUD code must obtain credentials
through :func:`get_dropbox_client` or :func:`dropbox_request`, never from the
environment directly, so the token source can later be swapped for OAuth here
without touching the CRUD layer.
"""

import os
from typing import Any

import dropbox
import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN_ENV_VAR = "DROPBOX_ACCESS_TOKEN"  # noqa: S105  (env var name, not a secret)
API_BASE_URL = "https://api.dropboxapi.com/2"
REQUEST_TIMEOUT_SECONDS = 30
HTTP_UNAUTHORIZED = 401


class DropboxError(RuntimeError):
    """Base class for errors raised by the Dropbox auth/client layer."""


class DropboxAuthError(DropboxError):
    """The access token is missing, invalid, or expired."""


class DropboxAPIError(DropboxError):
    """Dropbox returned a non-success response other than an auth failure."""

    def __init__(self, status_code: int, message: str) -> None:
        """Store the HTTP status code alongside the Dropbox error message."""
        super().__init__(f"Dropbox API error (HTTP {status_code}): {message}")
        self.status_code = status_code


class DropboxConnectionError(DropboxError):
    """The request never got a response (network failure, timeout, DNS, ...)."""


def get_access_token() -> str:
    """Return the Dropbox access token.

    Raises:
        DropboxAuthError: If ``DROPBOX_ACCESS_TOKEN`` is not set.

    """
    token = os.environ.get(ACCESS_TOKEN_ENV_VAR, "").strip()
    if not token:
        msg = (
            f"No valid credentials found. "
            f"Set {ACCESS_TOKEN_ENV_VAR} in your environment or .env file."
        )
        raise DropboxAuthError(msg)
    return token


def get_auth_headers() -> dict[str, str]:
    """Return the ``Authorization`` header for Dropbox API requests."""
    return {"Authorization": f"Bearer {get_access_token()}"}


def _error_summary(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or response.reason
    if isinstance(body, dict):
        return str(body.get("error_summary") or body.get("error") or body)
    return str(body)


def dropbox_request(endpoint: str, payload: dict[str, Any] | None = None) -> Any:  # noqa: ANN401
    """Make an authenticated POST request to a Dropbox RPC endpoint.

    Args:
        endpoint: API path such as ``"/users/get_current_account"``.
        payload: JSON arguments for the endpoint, or ``None`` if it takes none.

    Returns:
        The decoded JSON response, or ``None`` if the response body is empty.

    Raises:
        DropboxAuthError: If the token is missing, invalid, or expired.
        DropboxAPIError: If Dropbox returns any other error response.
        DropboxConnectionError: If the request fails before a response arrives.

    """
    url = f"{API_BASE_URL}/{endpoint.lstrip('/')}"
    try:
        response = requests.post(
            url,
            headers=get_auth_headers(),
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        msg = f"Could not reach Dropbox at {url}: {e}"
        raise DropboxConnectionError(msg) from e

    if response.status_code == HTTP_UNAUTHORIZED:
        msg = (
            f"Dropbox rejected the access token ({_error_summary(response)}). "
            f"Generate a new token and update {ACCESS_TOKEN_ENV_VAR}."
        )
        raise DropboxAuthError(msg)
    if not response.ok:
        raise DropboxAPIError(response.status_code, _error_summary(response))

    return response.json() if response.content else None


def get_current_account() -> dict[str, Any]:
    """Return the account the access token belongs to (useful as an auth check)."""
    account: dict[str, Any] = dropbox_request("/users/get_current_account")
    return account


def get_dropbox_client() -> dropbox.Dropbox:
    """Return an authenticated Dropbox SDK client for CRUD operations.

    Raises:
        DropboxAuthError: If no access token is configured.

    """
    return dropbox.Dropbox(
        oauth2_access_token=get_access_token(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def check_auth() -> bool:
    """Call ``users/get_current_account`` and print whether authentication works."""
    try:
        account = get_current_account()
    except DropboxError as e:
        print(f"Dropbox authentication FAILED: {e}")  # noqa: T201
        return False

    name = account.get("name", {}).get("display_name", "<unknown>")
    print(f"Dropbox authentication OK: {name} <{account.get('email', '<no email>')}>")  # noqa: T201
    return True
