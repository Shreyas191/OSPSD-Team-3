"""Entry point: start the Course Autopilot interface.

    uv run python web_main.py    # http://127.0.0.1:8002

``HOST`` and ``PORT`` change the address. The container image runs this file for the
interface, ``agent_main.py`` for the agent service and ``main.py`` for the API service.
"""

import os

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8002


def main() -> None:
    """Serve ``course_web.app``."""
    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    uvicorn.run("course_web.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
