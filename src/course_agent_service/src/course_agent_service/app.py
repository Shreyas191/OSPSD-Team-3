"""The agent service's HTTP app. This first version only reports its own health.

The agent loop, its tools and the approval flow are added on top of this app; they reach
providers only through the API service (``API_BASE_URL``), never directly.
"""

import os

from fastapi import FastAPI

from course_agent_service.config import Settings


def check_configuration() -> Settings:
    """Read the settings at startup, so a bad one stops the service before it serves."""
    return Settings.from_env()


app = FastAPI(
    title="Course Autopilot agent service",
    description="The model loop between the interface and the API service.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: answers without calling the model or the API service."""
    return {"status": "ok", "release": os.environ.get("RELEASE", "unknown")}
