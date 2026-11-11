"""Entry point: start the Cloud Storage Service.

    uv run python main.py    # http://127.0.0.1:8000, Swagger UI at /docs

``HOST`` and ``PORT`` change the address. The service needs Dropbox credentials (see the
README). Without them it stops before serving, with one line that names the missing
settings and exits with status 1. The container image runs this same file.
"""

import os

import uvicorn
from cloud_storage_service.app import ConfigurationError, check_configuration
from cloud_storage_service.telemetry import configure_logging

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main() -> None:
    """Check the configuration, then serve the FastAPI app in ``cloud_storage_service.app``."""
    try:
        check_configuration()
    except ConfigurationError as e:
        # One line on stderr, no traceback: the message names settings, never their values.
        msg = f"Startup failed: {e}"
        raise SystemExit(msg) from None
    configure_logging()
    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    # The service writes its own log line per request, so uvicorn's access log is off.
    uvicorn.run("cloud_storage_service.app:app", host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()
