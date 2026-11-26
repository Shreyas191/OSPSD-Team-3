"""Entry point: start the Course Autopilot agent service.

    uv run python agent_main.py    # http://127.0.0.1:8001

``HOST`` and ``PORT`` change the address. A missing or invalid setting stops the service
before it serves, with one line naming the setting. The container image runs this file for
the agent service and ``main.py`` for the API service.
"""

import os

import uvicorn
from course_agent_service.app import check_configuration
from course_agent_service.config import ConfigurationError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001


def main() -> None:
    """Check the configuration, then serve ``course_agent_service.app``."""
    try:
        check_configuration()
    except ConfigurationError as e:
        msg = f"Startup failed: {e}"
        raise SystemExit(msg) from None
    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    uvicorn.run("course_agent_service.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
