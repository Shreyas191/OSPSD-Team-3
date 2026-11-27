"""The interface's HTTP app. This first version serves the start page and its own health.

Pages are rendered on the server from the templates next to this file (DESIGN.md, P4). The
interface decides nothing and stores nothing: sign-in (#96) and the S1 pages (#95) will get
their data from the API and agent services.
"""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# The interface is pages for people, not an API, so it publishes no OpenAPI schema or docs.
app = FastAPI(
    title="Course Autopilot",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _release() -> str:
    return os.environ.get("RELEASE", "unknown")


@app.get("/", response_class=HTMLResponse)
def start_page(request: Request) -> HTMLResponse:
    """Render the start page: what Course Autopilot does, and which release is serving."""
    return templates.TemplateResponse(request, "index.html", {"release": _release()[:7]})


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: answers without calling the agent or API service."""
    return {"status": "ok", "release": _release()}
