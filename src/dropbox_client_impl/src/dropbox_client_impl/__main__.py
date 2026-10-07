"""Dropbox auth commands: ``uv run python -m dropbox_client_impl [login|logout|check]``.

- ``login``: authorize your Dropbox account via OAuth and save the credentials locally.
- ``logout``: revoke and delete the locally saved credentials.
- ``check`` (default): verify that authentication works.
"""

import sys
from collections.abc import Callable

from dropbox_client_impl.auth import check_auth, login, logout

COMMANDS: dict[str, Callable[[], bool]] = {"login": login, "logout": logout, "check": check_auth}

command = sys.argv[1] if len(sys.argv) > 1 else "check"
if command not in COMMANDS:
    sys.exit(f"Unknown command {command!r}. Use one of: {', '.join(COMMANDS)}.")
sys.exit(0 if COMMANDS[command]() else 1)
