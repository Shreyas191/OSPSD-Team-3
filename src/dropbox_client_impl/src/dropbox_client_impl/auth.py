"""Dropbox authentication (OAuth 2.0) and authenticated HTTP requests.

This is the only module that knows where Dropbox credentials come from. CRUD code must
obtain credentials through :func:`get_dropbox_client` or :func:`dropbox_request`, never
from the environment directly.

Credentials are resolved in this order:

1. A refresh token from ``DROPBOX_REFRESH_TOKEN`` (CI) or the local token file written by
   ``uv run python -m dropbox_client_impl login`` (default ``.dropbox_token.json``). It is
   combined with ``DROPBOX_APP_KEY`` / ``DROPBOX_APP_SECRET`` and the SDK refreshes
   short-lived access tokens automatically.
2. ``DROPBOX_ACCESS_TOKEN``: a manually generated access token (development fallback only).
"""

import json
import os
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlsplit

import dropbox
import requests
from dotenv import load_dotenv
from dropbox import oauth
from dropbox.exceptions import AuthError, HttpError

load_dotenv()

# Environment variable names (not secrets).
APP_KEY_ENV_VAR = "DROPBOX_APP_KEY"
APP_SECRET_ENV_VAR = "DROPBOX_APP_SECRET"  # noqa: S105
REDIRECT_URI_ENV_VAR = "DROPBOX_REDIRECT_URI"
REFRESH_TOKEN_ENV_VAR = "DROPBOX_REFRESH_TOKEN"  # noqa: S105
ACCESS_TOKEN_ENV_VAR = "DROPBOX_ACCESS_TOKEN"  # noqa: S105
TOKEN_FILE_ENV_VAR = "DROPBOX_TOKEN_FILE"  # noqa: S105
DEFAULT_TOKEN_FILE = ".dropbox_token.json"  # noqa: S105

LOGIN_COMMAND = "uv run python -m dropbox_client_impl login"
API_BASE_URL = "https://api.dropboxapi.com/2"
REQUEST_TIMEOUT_SECONDS = 30
LOGIN_TIMEOUT_SECONDS = 300
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNAUTHORIZED = 401
CSRF_SESSION_KEY = "dropbox-auth-csrf-token"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


class DropboxError(RuntimeError):
    """Base class for errors raised by the Dropbox auth/client layer."""


class DropboxAuthError(DropboxError):
    """Credentials are missing, invalid, revoked, or expired, or authorization failed."""


class DropboxAPIError(DropboxError):
    """Dropbox returned a non-success response other than an auth failure."""

    def __init__(self, status_code: int, message: str) -> None:
        """Store the HTTP status code alongside the Dropbox error message."""
        super().__init__(f"Dropbox API error (HTTP {status_code}): {message}")
        self.status_code = status_code


class DropboxConnectionError(DropboxError):
    """The request never got a response (network failure, timeout, DNS, ...)."""


# ----- Configuration and local credential storage -----


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _require_env(*names: str) -> list[str]:
    missing = [name for name in names if not _env(name)]
    if missing:
        msg = (
            f"Missing Dropbox OAuth configuration: {', '.join(missing)}. "
            "Set it in your environment or .env file (see .env.example)."
        )
        raise DropboxAuthError(msg)
    return [_env(name) for name in names]


def get_token_file() -> Path:
    """Return the path of the local OAuth credential file (``DROPBOX_TOKEN_FILE`` overrides it)."""
    return Path(_env(TOKEN_FILE_ENV_VAR) or DEFAULT_TOKEN_FILE)


def load_refresh_token() -> str | None:
    """Return the refresh token from ``DROPBOX_REFRESH_TOKEN`` or the token file, if any.

    Raises:
        DropboxAuthError: If the token file exists but cannot be parsed.

    """
    return _env(REFRESH_TOKEN_ENV_VAR) or _read_token_file(get_token_file())


def _read_token_file(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = str(data["refresh_token"]).strip()
    except (OSError, ValueError, KeyError, TypeError) as e:
        msg = f"Saved Dropbox credentials in {path} are unreadable. Run `{LOGIN_COMMAND}` again."
        raise DropboxAuthError(msg) from e
    return token or None


def save_refresh_token(refresh_token: str, account_id: str) -> Path:
    """Write the refresh token to the token file, readable only by the current user."""
    path = get_token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"refresh_token": refresh_token, "account_id": account_id}, indent=2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    path.chmod(0o600)
    return path


# ----- Authenticated clients and requests -----


def get_dropbox_client() -> dropbox.Dropbox:
    """Return an authenticated Dropbox SDK client for CRUD operations.

    No network call is made here; the SDK fetches an access token on first use.

    Raises:
        DropboxAuthError: If no credentials are configured, or a refresh token is found but
            ``DROPBOX_APP_KEY`` / ``DROPBOX_APP_SECRET`` are missing.

    """
    refresh_token = load_refresh_token()
    if refresh_token:
        app_key, app_secret = _require_env(APP_KEY_ENV_VAR, APP_SECRET_ENV_VAR)
        return dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    access_token = _env(ACCESS_TOKEN_ENV_VAR)
    if access_token:
        return dropbox.Dropbox(oauth2_access_token=access_token, timeout=REQUEST_TIMEOUT_SECONDS)

    msg = f"Dropbox is not authorized. Run `{LOGIN_COMMAND}` to connect your Dropbox account."
    raise DropboxAuthError(msg)


def get_access_token() -> str:
    """Return a currently valid Dropbox access token, refreshing it if needed.

    Raises:
        DropboxAuthError: If no credentials are configured or the refresh token was rejected.
        DropboxConnectionError: If Dropbox could not be reached to refresh the token.

    """
    dbx = get_dropbox_client()
    try:
        dbx.check_and_refresh_access_token()
    except AuthError as e:
        msg = (
            "Dropbox rejected the saved credentials (revoked or expired). "
            f"Run `{LOGIN_COMMAND}` again."
        )
        raise DropboxAuthError(msg) from e
    except requests.RequestException as e:
        msg = "Could not reach Dropbox to refresh the access token."
        raise DropboxConnectionError(msg) from e
    # The SDK exposes no public getter for the current access token.
    token: str = dbx._oauth2_access_token  # noqa: SLF001
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
        DropboxAuthError: If credentials are missing, invalid, or expired.
        DropboxAPIError: If Dropbox returns any other error response.
        DropboxConnectionError: If the request fails before a response arrives.

    """
    url = f"{API_BASE_URL}/{endpoint.lstrip('/')}"
    headers = get_auth_headers()
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        msg = f"Could not reach Dropbox at {url}: {e}"
        raise DropboxConnectionError(msg) from e

    if response.status_code == HTTP_UNAUTHORIZED:
        msg = (
            f"Dropbox rejected the access token ({_error_summary(response)}). "
            f"Run `{LOGIN_COMMAND}` again."
        )
        raise DropboxAuthError(msg)
    if not response.ok:
        raise DropboxAPIError(response.status_code, _error_summary(response))

    return response.json() if response.content else None


def get_current_account() -> dict[str, Any]:
    """Return the authenticated Dropbox account (useful as an auth check)."""
    account: dict[str, Any] = dropbox_request("/users/get_current_account")
    return account


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


# ----- OAuth authorization flow -----


def get_authorization_flow(session: dict[str, str]) -> oauth.DropboxOAuth2Flow:
    """Build the SDK OAuth flow from ``DROPBOX_APP_KEY``/``_APP_SECRET``/``_REDIRECT_URI``.

    ``token_access_type="offline"`` makes Dropbox return a long-lived refresh token.

    Raises:
        DropboxAuthError: If any of the three variables is missing.

    """
    app_key, app_secret, redirect_uri = _require_env(
        APP_KEY_ENV_VAR,
        APP_SECRET_ENV_VAR,
        REDIRECT_URI_ENV_VAR,
    )
    return oauth.DropboxOAuth2Flow(
        consumer_key=app_key,
        consumer_secret=app_secret,
        redirect_uri=redirect_uri,
        session=session,
        csrf_token_session_key=CSRF_SESSION_KEY,
        token_access_type="offline",  # noqa: S106
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def finish_authorization(flow: oauth.DropboxOAuth2Flow, query_params: dict[str, str]) -> str:
    """Exchange the redirect's query parameters for credentials and save them locally.

    Returns:
        The authorized Dropbox account ID.

    Raises:
        DropboxAuthError: If the user denied access or the redirect/code was invalid.

    """
    try:
        result = flow.finish(query_params)
    except oauth.NotApprovedException as e:
        msg = "Dropbox authorization was denied in the browser."
        raise DropboxAuthError(msg) from e
    except (
        oauth.BadRequestException,
        oauth.BadStateException,
        oauth.CsrfException,
        oauth.ProviderException,
        requests.RequestException,
    ) as e:
        msg = f"Dropbox authorization failed ({type(e).__name__}). Run `{LOGIN_COMMAND}` again."
        raise DropboxAuthError(msg) from e

    if not result.refresh_token:
        msg = "Dropbox did not return a refresh token; cannot keep you signed in."
        raise DropboxAuthError(msg)
    save_refresh_token(result.refresh_token, result.account_id)
    account_id: str = result.account_id
    return account_id


def _local_redirect_address(redirect_uri: str) -> tuple[str, int, str]:
    parsed = urlsplit(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS or parsed.port is None:
        msg = (
            f"{REDIRECT_URI_ENV_VAR} must be a local http URL with a port, "
            "for example http://localhost:8080/oauth/callback."
        )
        raise DropboxAuthError(msg)
    return parsed.hostname, parsed.port, parsed.path or "/"


def _wait_for_redirect(
    host: str,
    port: int,
    path: str,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Serve ``http://host:port/path`` until Dropbox redirects the browser there once."""
    params: dict[str, str] = {}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path != path:
                self.send_error(HTTP_NOT_FOUND)
                return
            params.update({key: values[0] for key, values in parse_qs(url.query).items()})
            body = (
                b"Dropbox authorization received. "
                b"You can close this tab and return to the terminal."
            )
            self.send_response(HTTP_OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401
            # The default handler logs the request line, which contains the authorization code.
            pass

    deadline = monotonic() + timeout
    with HTTPServer((host, port), _CallbackHandler) as server:
        while not params:
            remaining = deadline - monotonic()
            if remaining <= 0:
                msg = f"Timed out waiting for the Dropbox redirect to http://{host}:{port}{path}."
                raise DropboxAuthError(msg)
            server.timeout = remaining
            server.handle_request()
    return params


def login(open_browser: Callable[[str], object] = webbrowser.open) -> bool:
    """Run the interactive OAuth flow, save the refresh token, and verify it.

    Prints the authorization URL (and tries to open it in a browser), waits for Dropbox to
    redirect back to ``DROPBOX_REDIRECT_URI``, then stores the credentials in the token file.
    Tokens are never printed.
    """
    try:
        session: dict[str, str] = {}
        flow = get_authorization_flow(session)
        host, port, path = _local_redirect_address(flow.redirect_uri)
        url = flow.start()
        print("Open this URL to authorize Dropbox access (it should open automatically):")  # noqa: T201
        print(f"\n  {url}\n")  # noqa: T201
        open_browser(url)
        print(f"Waiting for Dropbox to redirect to {flow.redirect_uri} ...")  # noqa: T201
        finish_authorization(flow, _wait_for_redirect(host, port, path))
    except (DropboxError, OSError) as e:
        print(f"Dropbox login FAILED: {e}")  # noqa: T201
        return False

    print(f"Saved Dropbox credentials to {get_token_file()} (do not commit this file).")  # noqa: T201
    return check_auth()


def logout() -> bool:
    """Revoke the locally saved Dropbox credentials and delete the token file."""
    path = get_token_file()
    if not path.exists():
        print(f"No saved Dropbox credentials at {path}.")  # noqa: T201
        return True

    try:
        refresh_token = _read_token_file(path)
        if refresh_token:
            app_key, app_secret = _require_env(APP_KEY_ENV_VAR, APP_SECRET_ENV_VAR)
            dropbox.Dropbox(
                oauth2_refresh_token=refresh_token,
                app_key=app_key,
                app_secret=app_secret,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ).auth_token_revoke()
    except (DropboxError, HttpError, requests.RequestException) as e:
        print(  # noqa: T201
            f"Could not revoke the token with Dropbox ({type(e).__name__}); "
            "deleting it locally anyway.",
        )
    path.unlink()
    print(f"Deleted saved Dropbox credentials at {path}.")  # noqa: T201
    return True
