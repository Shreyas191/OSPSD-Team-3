"""Check Dropbox authentication: ``uv run python -m dropbox_client_impl``."""

import sys

from dropbox_client_impl.auth import check_auth

sys.exit(0 if check_auth() else 1)
