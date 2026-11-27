"""The interface skeleton: the start page renders, and health answers without other services."""

import pytest
from fastapi.testclient import TestClient

from course_web.app import app

client = TestClient(app)


def test_the_start_page_is_html_and_names_the_product() -> None:
    """The start page is HTML and names the product."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Course Autopilot</h1>" in response.text
    assert '<html lang="en">' in response.text


def test_the_start_page_shows_the_short_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start page shows the short release."""
    monkeypatch.setenv("RELEASE", "0123456789abcdef")

    response = client.get("/")

    assert "release <code>0123456</code>" in response.text
    assert "0123456789abcdef" not in response.text


def test_health_reports_the_full_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Health reports the full release."""
    monkeypatch.setenv("RELEASE", "0123456789abcdef")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "release": "0123456789abcdef"}


def test_health_without_a_release_says_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Health without a release says unknown."""
    monkeypatch.delenv("RELEASE", raising=False)

    assert client.get("/health").json()["release"] == "unknown"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_interface_publishes_no_api_docs(path: str) -> None:
    """The interface publishes no API docs."""
    assert client.get(path).status_code == 404
