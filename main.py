"""Entry point: start the Cloud Storage Service.

    uv run python main.py    # http://127.0.0.1:8000, Swagger UI at /docs

``HOST`` and ``PORT`` change the address. The service needs Dropbox credentials (see the
README); without them it stops at startup with an error that says how to log in.
"""

import os

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main() -> None:
    """Serve the FastAPI app in ``cloud_storage_service.app``."""
    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    uvicorn.run("cloud_storage_service.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
