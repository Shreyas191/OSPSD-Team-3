"""Fake credentials and small helpers shared by the ``dropbox_client_impl.auth`` unit tests.

The values are obviously fake and never reach Dropbox: every test that uses them mocks
the SDK or HTTP layer. ``SECRETS`` lists the values that must never appear in error
messages, printed output or logs; ``assert_no_secrets`` checks that.
"""

# ``tests`` is deliberately not a package (pytest runs with --import-mode=importlib), the
# same reason test_*.py files are exempt from INP001 in pyproject.toml.
# ruff: noqa: INP001

import json
from pathlib import Path

from dropbox import oauth

from dropbox_client_impl import auth

APP_KEY = "test-app-key"
APP_SECRET = "test-app-secret-value"  # noqa: S105 - fake value
REDIRECT_URI = "http://localhost:8080/oauth/callback"
REFRESH_TOKEN = "test-refresh-token-value"  # noqa: S105 - fake value
ACCESS_TOKEN = "test-access-token-value"  # noqa: S105 - fake value
OAUTH_ENV = {
    "DROPBOX_APP_KEY": APP_KEY,
    "DROPBOX_APP_SECRET": APP_SECRET,
    "DROPBOX_REDIRECT_URI": REDIRECT_URI,
}
SECRETS = (APP_SECRET, REFRESH_TOKEN, ACCESS_TOKEN)


def write_token_file(token: str = REFRESH_TOKEN) -> Path:
    """Save ``token`` to the (test-local) token file, as ``login`` would, and return its path."""
    path = auth.get_token_file()
    path.write_text(json.dumps({"refresh_token": token, "account_id": "dbid:1"}))
    return path


def assert_no_secrets(*texts: str) -> None:
    """Fail if any fake secret appears in any of ``texts``."""
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


def flow_result(refresh_token: str | None = REFRESH_TOKEN) -> oauth.OAuth2FlowNoRedirectResult:
    """Return what Dropbox's token endpoint would give back for an authorization code."""
    return oauth.OAuth2FlowNoRedirectResult(ACCESS_TOKEN, "dbid:1", "uid", refresh_token, None, None)
