"""Connecting, choosing a Trello list and disconnecting, against a fake API service."""

import logging
from html import unescape

import httpx
import pytest
from fastapi.testclient import TestClient

from course_web.app import SESSION_COOKIE, HideCallbackQueries

from .conftest import GOOD_CODES, PROVIDER_URLS, SESSION_TOKEN, FakeApi, csrf


def _signed_in(browser: TestClient) -> str:
    browser.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
    return csrf(browser)


def _text(response: httpx.Response) -> str:
    return unescape(response.text)


def _connection_calls(fake_api: FakeApi) -> list[tuple[str, str, str | None]]:
    return [(m, p, t) for m, p, t, _ in fake_api.calls if p.startswith("/connections")]


def test_the_page_lists_each_account_with_what_to_do(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Each provider shows its state and the one action that fits it."""
    fake_api.connected = {"dropbox": "connected", "google_calendar": "needs_attention"}
    _signed_in(browser)

    page = _text(browser.get("/connections"))

    assert "Connected as student@example.com" in page
    assert "Needs reconnecting" in page
    assert 'action="/connections/google_calendar/connect"' in page  # reconnect
    assert 'action="/connections/trello/connect"' in page  # not connected yet
    assert 'action="/connections/dropbox/disconnect"' in page
    assert 'action="/connections/dropbox/connect"' not in page  # already connected
    assert ("GET", "/connections", SESSION_TOKEN) in _connection_calls(fake_api)


def test_signed_out_the_page_sends_you_to_sign_in(fake_api: FakeApi, browser: TestClient) -> None:
    """Without a session, the connections page goes back to the start page, calling nothing."""
    response = browser.get("/connections")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert fake_api.calls == []


@pytest.mark.parametrize("provider", ["dropbox", "google_calendar", "trello"])
def test_connect_sends_the_browser_to_the_provider(
    fake_api: FakeApi,
    browser: TestClient,
    provider: str,
) -> None:
    """Connect starts the attempt at the API with the session, then redirects to the provider."""
    token = _signed_in(browser)

    response = browser.post(f"/connections/{provider}/connect", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == f"{PROVIDER_URLS[provider]}?state=st-{provider}"
    assert ("POST", f"/connections/{provider}/start", SESSION_TOKEN) in _connection_calls(
        fake_api,
    )


def test_connect_without_the_csrf_value_calls_nothing(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """A connect form without this browser's CSRF value is refused before the API is called."""
    _signed_in(browser)

    response = browser.post("/connections/dropbox/connect", data={"csrf_token": "forged"})

    assert response.status_code == 403
    assert _connection_calls(fake_api) == []


def test_an_unknown_provider_is_not_found_and_calls_nothing(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Only the three providers can be connected; anything else is a 404 page."""
    token = _signed_in(browser)

    response = browser.post("/connections/github/connect", data={"csrf_token": token})

    assert response.status_code == 404
    assert _connection_calls(fake_api) == []


@pytest.mark.parametrize("provider", ["dropbox", "google_calendar"])
def test_the_callback_finishes_the_connection(
    fake_api: FakeApi,
    browser: TestClient,
    provider: str,
) -> None:
    """The provider's callback is finished at the API with the session, then shows the list."""
    _signed_in(browser)
    params = {"state": f"st-{provider}", "code": GOOD_CODES[provider]}

    response = browser.get(f"/connections/{provider}/callback", params=params)

    assert response.status_code == 303
    assert response.headers["location"] == "/connections"
    assert fake_api.connected[provider] == "connected"
    assert ("POST", f"/connections/{provider}/finish", SESSION_TOKEN) in _connection_calls(
        fake_api,
    )


def test_a_declined_connection_says_nothing_was_saved(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """When the person says no at the provider, nothing is finished at the API."""
    _signed_in(browser)

    response = browser.get(
        "/connections/dropbox/callback",
        params={"state": "st-dropbox", "error": "access_denied"},
    )

    assert response.status_code == 400
    assert "Dropbox wasn't connected" in _text(response)
    assert 'href="/connections"' in response.text
    assert not any(p.endswith("/finish") for _, p, _ in _connection_calls(fake_api))


def test_a_refused_finish_saves_nothing_and_points_back(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """A callback the API refuses (another session's, used, or expired) connects nothing."""
    _signed_in(browser)

    response = browser.get(
        "/connections/dropbox/callback",
        params={"state": "someone-elses", "code": "dbx-code"},
    )

    assert response.status_code == 400
    assert "Dropbox didn't connect" in _text(response)
    assert 'href="/connections"' in response.text
    assert "dropbox" not in fake_api.connected


def test_the_trello_return_page_posts_the_token_from_its_script(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Trello's return page carries the state and a script, and finishes nothing by itself."""
    _signed_in(browser)

    page = browser.get("/connections/trello/callback", params={"state": "st-trello"})
    script = browser.get("/static/trello-return.js")

    assert page.status_code == 200
    assert (
        '<form id="trello-return" method="post" action="/connections/trello/finish">' in page.text
    )
    assert '<input type="hidden" name="state" value="st-trello">' in page.text
    assert '<script src="/static/trello-return.js"></script>' in page.text
    assert "<script>" not in page.text  # nothing inline: the CSP allows scripts from /static
    assert script.status_code == 200
    assert "location.hash" in script.text
    assert "replaceState" in script.text  # the token leaves the address bar
    assert "if (!token)" in script.text  # nothing to post: Back after connecting, or declined
    assert '<p id="trello-nothing" hidden>' in page.text
    assert not any(p.endswith("/finish") for _, p, _ in _connection_calls(fake_api))


def test_the_trello_token_finishes_and_asks_for_a_list(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """The posted Trello token finishes the connection; choosing a list comes next."""
    token = _signed_in(browser)

    response = browser.post(
        "/connections/trello/finish",
        data={"csrf_token": token, "state": "st-trello", "token": "trello-token"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/connections/trello/list"
    assert fake_api.connected["trello"] == "setup_needed"


def test_the_trello_token_needs_the_csrf_value(fake_api: FakeApi, browser: TestClient) -> None:
    """A forged Trello finish (no CSRF value) is refused before the API is called."""
    _signed_in(browser)

    response = browser.post(
        "/connections/trello/finish",
        data={"state": "st-trello", "token": "attacker-token"},
    )

    assert response.status_code == 403
    assert "trello" not in fake_api.connected


def test_choosing_a_trello_list(fake_api: FakeApi, browser: TestClient) -> None:
    """The lists are grouped by board, and the chosen one is saved at the API."""
    fake_api.connected["trello"] = "setup_needed"
    fake_api.trello_lists.append({"board": "<b>x</b>", "id": "list-4", "name": "<i>y</i>"})
    token = _signed_in(browser)

    page = browser.get("/connections/trello/list")
    chosen = browser.post(
        "/connections/trello/list",
        data={"csrf_token": token, "list_id": "list-2"},
    )

    assert "<legend>Fall 2026</legend>" in page.text
    assert 'value="list-2"' in page.text
    assert "<b>x</b>" not in page.text  # board and list names are escaped
    assert "&lt;i&gt;y&lt;/i&gt;" in page.text
    assert chosen.status_code == 303
    assert chosen.headers["location"] == "/connections"
    assert fake_api.connected["trello"] == "connected"


def test_disconnect_forgets_the_account(fake_api: FakeApi, browser: TestClient) -> None:
    """Disconnect deletes the connection at the API and returns to the list."""
    fake_api.connected["dropbox"] = "connected"
    token = _signed_in(browser)

    response = browser.post("/connections/dropbox/disconnect", data={"csrf_token": token})

    assert response.status_code == 303
    assert "dropbox" not in fake_api.connected
    assert ("DELETE", "/connections/dropbox", SESSION_TOKEN) in _connection_calls(fake_api)


def test_connections_off_on_the_service_says_so(fake_api: FakeApi, browser: TestClient) -> None:
    """When the API has no connections set up, the page says so instead of failing."""
    fake_api.fail["GET /connections"] = (503, "connections_not_configured")
    _signed_in(browser)

    response = browser.get("/connections")

    assert response.status_code == 503
    assert "Connecting accounts isn't open on this site yet" in _text(response)


def test_the_policy_lets_connect_forms_reach_the_providers(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """The connect forms redirect to Dropbox and Trello, so form-action allows exactly those."""
    del fake_api
    policy = browser.get("/").headers["content-security-policy"]
    form_action = next(p for p in policy.split("; ") if p.startswith("form-action"))

    assert form_action.split() == [
        "form-action",
        "'self'",
        "https://accounts.google.com",
        "https://www.dropbox.com",
        "https://trello.com",
    ]


def test_the_first_screen_prompt_links_to_connections(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """A new user's prompt links to the connections page, unless connections are off."""
    _signed_in(browser)
    fake_api.fail["GET /courses"] = (409, "not_connected")
    new_user = browser.get("/").text
    fake_api.fail["GET /courses"] = (503, "connections_not_configured")
    switched_off = browser.get("/").text

    assert '<a href="/connections">Connect your accounts</a>' in new_user
    assert '<a href="/connections">Connect your accounts</a>' not in switched_off


# ----- Review of #129 -----


@pytest.mark.parametrize(
    ("route", "method", "path"),
    [
        ("POST /connections/trello/start", "post", "/connections/trello/connect"),
        ("GET /connections/trello/lists", "get", "/connections/trello/list"),
    ],
)
def test_one_provider_not_set_up_says_so_and_logs_no_api_error(  # noqa: PLR0913 (fixtures and case)
    fake_api: FakeApi,
    browser: TestClient,
    caplog: pytest.LogCaptureFixture,
    route: str,
    method: str,
    path: str,
) -> None:
    """The API's 503 ``connection_not_configured`` for one provider isn't shown as an outage."""
    fake_api.fail[route] = (503, "connection_not_configured")
    token = _signed_in(browser)

    with caplog.at_level(logging.WARNING, logger="course_web"):
        if method == "post":
            response = browser.post(path, data={"csrf_token": token})
        else:
            response = browser.get(path)

    assert response.status_code == 503
    assert "Connecting Trello isn't available on this site yet" in _text(response)
    assert "can't reach" not in _text(response)
    assert 'href="/connections"' in response.text
    assert caplog.records == []  # not an API failure: nothing to investigate


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/connections/dropbox/disconnect", {}),
        ("/connections/trello/list", {"list_id": "list-2"}),
    ],
)
def test_disconnect_and_choosing_a_list_need_the_csrf_value(
    fake_api: FakeApi,
    browser: TestClient,
    path: str,
    data: dict[str, str],
) -> None:
    """A forged disconnect or list choice (no CSRF value) is refused before the API is called."""
    fake_api.connected = {"dropbox": "connected", "trello": "setup_needed"}
    token = _signed_in(browser)

    forged = browser.post(path, data=data)

    assert forged.status_code == 403
    assert _connection_calls(fake_api) == []
    assert fake_api.connected == {"dropbox": "connected", "trello": "setup_needed"}
    # Control: the same request with the value goes through.
    assert browser.post(path, data={**data, "csrf_token": token}).status_code == 303


def test_account_labels_and_trello_names_are_escaped(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Names that come from the person's providers are shown as text, never as markup."""
    hostile = '<img src=x onerror="alert(1)">'
    fake_api.connected = {"dropbox": "connected", "trello": "setup_needed"}
    fake_api.account = hostile
    fake_api.trello_lists = [{"board": hostile, "id": "list-1", "name": hostile}]
    _signed_in(browser)

    pages = [browser.get("/connections").text, browser.get("/connections/trello/list").text]

    for page in pages:
        assert hostile not in page
        assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in page
    assert f"Connected as {hostile}" in unescape(pages[0])  # control: the label is shown


def test_the_trello_token_is_in_no_page_and_no_log_record(
    fake_api: FakeApi,
    browser: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """From connect to choosing a list, the Trello token reaches the API and nowhere else."""
    trello_token = GOOD_CODES["trello"]
    token = _signed_in(browser)

    with caplog.at_level(logging.DEBUG):
        responses = [
            browser.post("/connections/trello/connect", data={"csrf_token": token}),
            browser.get("/connections/trello/callback", params={"state": "st-trello"}),
            browser.post(
                "/connections/trello/finish",
                data={"csrf_token": token, "state": "st-trello", "token": trello_token},
            ),
            browser.get("/connections/trello/list"),
            browser.post(
                "/connections/trello/list",
                data={"csrf_token": token, "list_id": "list-1"},
            ),
            browser.get("/connections"),
        ]

    for response in responses:
        assert trello_token not in response.text
        assert trello_token not in str(response.headers)
    assert trello_token not in caplog.text
    # Control: the token did reach the API, and the steps logged something.
    finish = [b for _, p, _, b in fake_api.calls if p == "/connections/trello/finish"]
    assert finish == [{"state": "st-trello", "code": trello_token}]
    assert caplog.records
    assert fake_api.connected["trello"] == "connected"


def test_a_connection_that_needs_attention_offers_reconnect_and_disconnect(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """A provider that stopped accepting our access can be reconnected or removed."""
    fake_api.connected = {"trello": "needs_attention"}
    _signed_in(browser)

    page = _text(browser.get("/connections"))

    assert "Needs reconnecting" in page
    assert 'action="/connections/trello/connect"' in page
    assert ">Reconnect</button>" in page
    assert 'action="/connections/trello/disconnect"' in page
    assert 'action="/connections/dropbox/disconnect"' not in page  # control: not connected


def test_the_trello_return_page_signed_out_shows_no_signed_in_header(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Without a session, the return page doesn't offer the signed-in navigation."""
    signed_out = browser.get("/connections/trello/callback", params={"state": "st-trello"})
    calls_signed_out = list(fake_api.calls)
    _signed_in(browser)
    signed_in = browser.get("/connections/trello/callback", params={"state": "st-trello"})

    assert 'action="/sign-out"' not in signed_out.text
    assert 'action="/sign-out"' in signed_in.text  # control
    assert calls_signed_out == []  # the return page itself calls nothing


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        ("/auth/callback?state=s1&code=secret-code", "/auth/callback?[hidden]"),
        (
            "/connections/dropbox/callback?state=s&code=secret-code",
            "/connections/dropbox/callback?[hidden]",
        ),
        ("/connections?notice=secret-code", "/connections?notice=secret-code"),  # not a callback
        ("/auth/callback", "/auth/callback"),
    ],
)
def test_the_access_log_hides_callback_queries(
    caplog: pytest.LogCaptureFixture,
    path: str,
    logged: str,
) -> None:
    """Uvicorn's access line for a callback keeps the path and drops the codes."""
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, HideCallbackQueries) for f in access.filters)

    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        # The call uvicorn 0.54 makes for each response (protocols/http/h11_impl.py).
        access.info('%s - "%s %s HTTP/%s" %d', "203.0.113.7:5000", "GET", path, "1.1", 303)

    assert caplog.messages == [f'203.0.113.7:5000 - "GET {logged} HTTP/1.1" 303']
