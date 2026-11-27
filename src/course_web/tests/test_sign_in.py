"""Sign-in, sign-out and the signed-in course list, against a fake API service."""

from html import unescape

import httpx
from fastapi.testclient import TestClient

from course_web.app import CSRF_COOKIE, SESSION_COOKIE, SIGN_IN_COOKIE

from .conftest import GOOGLE_URL, SESSION_TOKEN, FakeApi, csrf


def _signed_in(browser: TestClient) -> None:
    browser.cookies.set(SESSION_COOKIE, SESSION_TOKEN)


def _text(response: httpx.Response) -> str:
    return unescape(response.text)


def _set_cookie_headers(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def test_the_start_page_offers_sign_in_when_the_api_is_configured(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """The start page offers sign-in when the API is configured."""
    response = browser.get("/")

    assert response.status_code == 200
    assert '<form method="post" action="/sign-in">' in response.text
    assert "Sign in with Google" in response.text
    assert fake_api.calls == []


def test_the_csrf_cookie_is_httponly_secure_and_lax(fake_api: FakeApi, browser: TestClient) -> None:
    """The CSRF cookie is HttpOnly, Secure and SameSite=Lax."""
    del fake_api
    (cookie,) = [c for c in _set_cookie_headers(browser.get("/")) if c.startswith(CSRF_COOKIE)]

    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie


def test_sign_in_sends_the_browser_to_google_and_keeps_the_binding(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Sign in sends the browser to google and keeps the binding."""
    response = browser.post("/sign-in", data={"csrf_token": csrf(browser)})

    assert response.status_code == 303
    assert response.headers["location"] == GOOGLE_URL
    (cookie,) = [c for c in _set_cookie_headers(response) if c.startswith(SIGN_IN_COOKIE)]
    assert cookie.startswith(f"{SIGN_IN_COOKIE}=b1;")
    assert "HttpOnly" in cookie
    assert "Max-Age=600" in cookie
    assert ("POST", "/auth/google/start", None, None) in fake_api.calls


def test_a_form_without_the_csrf_value_is_refused_and_nothing_is_called(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """A form without the csrf value is refused and nothing is called."""
    csrf(browser)

    missing = browser.post("/sign-in", data={})
    wrong = browser.post("/sign-in", data={"csrf_token": "not-the-cookie"})

    assert missing.status_code == wrong.status_code == 403
    assert "This form expired" in _text(wrong)
    assert fake_api.calls == []


def test_a_form_from_a_browser_without_the_cookie_is_refused(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """A form from a browser without the cookie is refused (another site posting)."""
    response = browser.post("/sign-out", data={"csrf_token": "anything"})

    assert response.status_code == 403
    assert fake_api.calls == []


def test_the_callback_finishes_sign_in_and_sets_the_session_cookie(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """The callback finishes sign in and sets the session cookie."""
    browser.cookies.set(SIGN_IN_COOKIE, "b1")

    response = browser.get("/auth/callback", params={"state": "s1", "code": "c1"})

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookies = _set_cookie_headers(response)
    (session,) = [c for c in cookies if c.startswith(f"{SESSION_COOKIE}=")]
    assert session.startswith(f"{SESSION_COOKIE}={SESSION_TOKEN};")
    assert "HttpOnly" in session
    assert "Secure" in session
    assert f"Max-Age={7 * 24 * 60 * 60}" in session
    (binding,) = [c for c in cookies if c.startswith(f"{SIGN_IN_COOKIE}=")]
    assert "Max-Age=0" in binding
    finish = ("POST", "/auth/google/finish", None, {"state": "s1", "code": "c1", "binding": "b1"})
    assert finish in fake_api.calls


def test_the_callback_without_the_binding_cookie_does_not_call_the_api(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """The callback without the binding cookie does not call the API (another browser)."""
    response = browser.get("/auth/callback", params={"state": "s1", "code": "c1"})

    assert response.status_code == 400
    assert "started in another browser" in _text(response)
    assert SESSION_COOKIE not in browser.cookies
    assert fake_api.calls == []


def test_a_cancelled_sign_in_says_so(fake_api: FakeApi, browser: TestClient) -> None:
    """A cancelled sign in says so."""
    browser.cookies.set(SIGN_IN_COOKIE, "b1")

    response = browser.get("/auth/callback", params={"error": "access_denied", "state": "s1"})

    assert response.status_code == 400
    assert "Sign-in was cancelled" in _text(response)
    assert fake_api.calls == []


def test_a_refused_finish_gives_no_session(fake_api: FakeApi, browser: TestClient) -> None:
    """A refused finish gives no session, and the attempt's cookie is cleared."""
    browser.cookies.set(SIGN_IN_COOKIE, "other-binding")

    response = browser.get("/auth/callback", params={"state": "s1", "code": "c1"})

    assert response.status_code == 400
    assert "Sign-in didn't complete" in _text(response)
    assert not any(c.startswith(f"{SESSION_COOKIE}=") for c in _set_cookie_headers(response))
    assert any(
        c.startswith(f"{SIGN_IN_COOKIE}=") and "Max-Age=0" in c
        for c in _set_cookie_headers(response)
    )


def test_signed_in_the_start_page_lists_the_courses_with_the_session_token(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Signed in, the start page lists the courses, asked for with the session token."""
    fake_api.courses = [
        {"course": "CS-GY 6083", "has_folder": True, "events": 9, "issues": 9, "documents": 1},
    ]
    _signed_in(browser)

    response = browser.get("/")

    assert response.status_code == 200
    assert "<h1>Your courses</h1>" in response.text
    assert "<td>CS-GY 6083</td>" in response.text
    assert 'action="/sign-out"' in response.text
    assert fake_api.calls == [("GET", "/courses", SESSION_TOKEN, None)]


def test_signed_in_without_courses_says_what_will_appear(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Signed in without courses, the page says what will appear there."""
    del fake_api
    _signed_in(browser)

    assert "You have no courses yet" in browser.get("/").text


def test_course_names_are_escaped(fake_api: FakeApi, browser: TestClient) -> None:
    """Course names from the API are escaped, never run as HTML."""
    fake_api.courses = [
        {
            "course": "<script>x</script>",
            "has_folder": True,
            "events": 0,
            "issues": 0,
            "documents": 0,
        },
    ]
    _signed_in(browser)

    text = browser.get("/").text

    assert "<script>x</script>" not in text
    assert "&lt;script&gt;x&lt;/script&gt;" in text


def test_an_ended_session_shows_sign_in_again_and_clears_the_cookie(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """An ended session shows sign in again and clears the cookie."""
    browser.cookies.set(SESSION_COOKIE, "expired-token")

    response = browser.get("/")

    assert response.status_code == 200
    assert "Sign in with Google" in response.text
    assert any(
        c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in c
        for c in _set_cookie_headers(response)
    )
    assert fake_api.calls == [("GET", "/courses", "expired-token", None)]


def test_an_unreachable_api_says_nothing_changed_and_to_retry(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """An unreachable API says nothing was changed and to try again."""
    fake_api.down = True
    _signed_in(browser)

    response = browser.get("/")

    assert response.status_code == 503
    assert "can't reach its service" in _text(response)
    assert "Nothing was changed. Try again in a minute." in _text(response)


def test_sign_in_not_configured_at_the_api_says_so(fake_api: FakeApi, browser: TestClient) -> None:
    """Sign-in not configured at the API says so."""
    fake_api.fail["POST /auth/google/start"] = (503, "sign_in_not_configured")

    response = browser.post("/sign-in", data={"csrf_token": csrf(browser)})

    assert response.status_code == 503
    assert "Sign-in isn't open on this site yet" in _text(response)


def test_sign_out_ends_the_session_at_the_api_and_clears_the_cookie(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Sign out ends the session at the API and clears the cookie."""
    _signed_in(browser)
    token = csrf(browser)
    fake_api.calls.clear()

    response = browser.post("/sign-out", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert fake_api.calls == [("POST", "/auth/sign-out", SESSION_TOKEN, None)]
    assert SESSION_TOKEN not in fake_api.valid_tokens
    assert any(
        c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in c
        for c in _set_cookie_headers(response)
    )


def test_sign_out_clears_the_cookie_even_when_the_api_is_down(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Sign out clears the cookie even when the API is down."""
    token = csrf(browser)
    _signed_in(browser)
    fake_api.down = True

    response = browser.post("/sign-out", data={"csrf_token": token})

    assert response.status_code == 303
    assert any(
        c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in c
        for c in _set_cookie_headers(response)
    )


def test_every_page_has_the_security_headers(fake_api: FakeApi, browser: TestClient) -> None:
    """Every page has the security headers: no framing, no inline code, no referrer or cache."""
    del fake_api
    for response in (browser.get("/"), browser.get("/health"), browser.get("/auth/callback")):
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_without_an_api_sign_in_and_callback_say_sign_in_is_not_open(browser: TestClient) -> None:
    """Without an API service, sign-in and the callback say sign-in isn't open."""
    response = browser.post("/sign-in", data={"csrf_token": csrf(browser)})
    callback = browser.get("/auth/callback", params={"state": "s", "code": "c"})

    assert response.status_code == callback.status_code == 503
    assert "Sign-in isn't open on this site yet" in _text(callback)
