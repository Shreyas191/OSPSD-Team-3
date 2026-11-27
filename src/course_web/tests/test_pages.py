"""The stylesheet, and the interface's one public address (PUBLIC_URL)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from course_web.app import app
from course_web.config import Settings

PUBLIC = "https://course-web-test-123.us-central1.run.app"


def test_styles_come_from_the_stylesheet_not_the_page() -> None:
    """Pages link the stylesheet and carry no inline style, which the CSP would block."""
    browser = TestClient(app, base_url="https://testserver")
    page = browser.get("/")
    css = browser.get("/static/site.css")

    assert '<link rel="stylesheet" href="/static/site.css">' in page.text
    assert "<style" not in page.text
    assert "style=" not in page.text
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "--accent" in css.text
    assert css.headers["x-frame-options"] == "DENY"


@pytest.fixture
def public_url() -> Iterator[None]:
    """Run the app with PUBLIC_URL set."""
    app.state.settings = Settings(public_url=PUBLIC)
    yield
    del app.state.settings


def _at(host: str) -> TestClient:
    return TestClient(app, base_url=f"https://{host}", follow_redirects=False)


@pytest.mark.usefixtures("public_url")
def test_a_page_on_the_other_address_is_sent_to_the_public_one() -> None:
    """A page asked for on the hash-based address is sent, path and query kept, to PUBLIC_URL."""
    response = _at("course-web-test-abc123-uc.a.run.app").get("/auth/callback?state=s&code=c")

    assert response.status_code == 308
    assert response.headers["location"] == f"{PUBLIC}/auth/callback?state=s&code=c"
    assert response.headers["x-frame-options"] == "DENY"  # redirects carry the headers too


@pytest.mark.usefixtures("public_url")
def test_the_public_address_is_served() -> None:
    """The control: on the public address the page is served."""
    response = _at("course-web-test-123.us-central1.run.app").get("/")

    assert response.status_code == 200
    assert "Course Autopilot" in response.text


@pytest.mark.usefixtures("public_url")
def test_a_revision_tag_url_and_health_are_served_where_they_are() -> None:
    """The deploy's smoke check (a tag URL) and /health work on any address."""
    tagged = _at("rel-abc1234---course-web-test-123.us-central1.run.app").get("/")
    health = _at("course-web-test-abc123-uc.a.run.app").get("/health")

    assert tagged.status_code == 200
    assert health.status_code == 200


def test_without_a_public_url_every_address_is_served() -> None:
    """On a laptop (no PUBLIC_URL), nothing is redirected."""
    assert _at("localhost:8002").get("/").status_code == 200
