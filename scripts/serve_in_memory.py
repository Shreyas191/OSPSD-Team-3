"""Serve the real HTTP app on an in-memory storage client, with no Dropbox and no credentials.

    uv run python scripts/serve_in_memory.py [port]      # default port 8000

CI runs the vertical's conformance suite against it on every pull request (see
docs/testing.md). Everything is lost when the process stops.
"""

# ruff: noqa: INP001 - a command-line script, not part of a package

import importlib
import sys
from pathlib import Path
from typing import Any

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.memory_client import InMemoryClient

DEFAULT_PORT = 8000

if __name__ == "__main__":
    # ``cloud_storage_service.app`` is both a module and the app object it exports, so get
    # the module itself to replace its provider wiring.
    app_module: Any = importlib.import_module("cloud_storage_service.app")
    app_module.build_storage_client = InMemoryClient
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    uvicorn.run(app_module.app, host="127.0.0.1", port=port, log_level="warning")
