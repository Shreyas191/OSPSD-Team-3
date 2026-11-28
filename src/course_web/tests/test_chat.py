"""Conversations with the assistant, against fake API and agent services."""

from html import unescape

import pytest
from fastapi.testclient import TestClient

from course_web.app import MAX_MESSAGE_CHARACTERS, SESSION_COOKIE, UNAVAILABLE

from .conftest import AGENT_TOKEN, EVENT, SESSION_TOKEN, FakeAgent, FakeApi, csrf


def _signed_in(browser: TestClient) -> str:
    browser.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
    return csrf(browser)


def _new_conversation(fake_api: FakeApi) -> str:
    conversation_id = "0b0e4e8e-5c38-4f4c-9d0e-3f1e1c3b9a10"
    fake_api.conversations[conversation_id] = []
    return conversation_id


def test_the_course_page_offers_the_assistant(fake_api: FakeApi, browser: TestClient) -> None:
    """Signed in, with courses readable, the start page has a button to start a conversation."""
    _signed_in(browser)

    page = browser.get("/").text

    assert '<form method="post" action="/chat">' in page
    assert fake_api.calls


def test_starting_a_conversation_goes_to_it(fake_api: FakeApi, browser: TestClient) -> None:
    """The button creates a conversation at the API with the session, then shows it."""
    token = _signed_in(browser)

    response = browser.post("/chat", data={"csrf_token": token})

    [conversation_id] = fake_api.conversations
    assert response.status_code == 303
    assert response.headers["location"] == f"/chat/{conversation_id}"
    assert ("POST", "/conversations", SESSION_TOKEN, {"title": ""}) in fake_api.calls


def test_starting_a_conversation_needs_the_csrf_value(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """A forged form can't start a conversation."""
    _signed_in(browser)

    response = browser.post("/chat", data={"csrf_token": "forged"})

    assert response.status_code == 403
    assert fake_api.conversations == {}


def test_a_turn_goes_to_the_agent_with_an_agent_token_only(
    fake_api: FakeApi,
    fake_agent: FakeAgent,
    browser: TestClient,
) -> None:
    """The agent gets a token made for this turn, never the person's session token."""
    token = _signed_in(browser)
    conversation_id = _new_conversation(fake_api)

    response = browser.post(
        f"/chat/{conversation_id}",
        data={"csrf_token": token, "text": "Set up CS-GY 6083"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/chat/{conversation_id}"
    assert ("POST", "/auth/agent-token", SESSION_TOKEN, None) in fake_api.calls
    [(path, sent_token, body)] = fake_agent.calls
    assert path == f"/conversations/{conversation_id}/turns"
    assert sent_token == AGENT_TOKEN
    assert body == {"text": "Set up CS-GY 6083"}


def test_the_conversation_shows_messages_and_replies_not_the_agents_working(
    fake_api: FakeApi,
    fake_agent: FakeAgent,
    browser: TestClient,
) -> None:
    """The student's message and the reply are shown; tool calls and results aren't."""
    token = _signed_in(browser)
    conversation_id = _new_conversation(fake_api)
    browser.post(f"/chat/{conversation_id}", data={"csrf_token": token, "text": "Hello"})

    page = unescape(browser.get(f"/chat/{conversation_id}").text)

    assert "Hello" in page
    assert "Which course?" in page
    assert "list_courses" not in page
    assert page.count('class="said"') == 2
    assert fake_agent.calls


def test_a_reply_that_prepared_changes_links_to_them(
    fake_api: FakeApi,
    fake_agent: FakeAgent,
    browser: TestClient,
) -> None:
    """A turn that prepared a proposal shows a link to review it, also after a reload."""
    fake_agent.propose = [EVENT]
    token = _signed_in(browser)
    conversation_id = _new_conversation(fake_api)
    browser.post(f"/chat/{conversation_id}", data={"csrf_token": token, "text": "Set it up"})

    page = browser.get(f"/chat/{conversation_id}").text

    [proposal_id] = fake_api.proposals
    link = f'<a href="/proposals/{proposal_id}?conversation={conversation_id}">'
    assert f"{link}Review the proposed changes</a>" in page


def test_model_text_is_shown_as_text(fake_api: FakeApi, browser: TestClient) -> None:
    """Markup in a reply is escaped, so a reply can't add links or load anything."""
    _signed_in(browser)
    conversation_id = _new_conversation(fake_api)
    fake_api.conversations[conversation_id].append(
        {"role": "assistant", "content": {"text": '<a href="https://x.test">win</a><img src=x>'}},
    )

    page = browser.get(f"/chat/{conversation_id}").text

    assert '<a href="https://x.test">' not in page
    assert "<img src=x>" not in page
    assert "&lt;a href=" in page


@pytest.mark.parametrize(
    ("status", "code", "shown"),
    [
        (429, "assistant_busy", "The assistant is busy right now"),
        (429, "daily_limit_reached", "You've reached today's limit"),
        (503, "assistant_disabled", UNAVAILABLE),
        (503, "unreachable", UNAVAILABLE),
    ],
)
def test_a_refused_turn_says_why_and_keeps_the_message(  # noqa: PLR0913 (fixtures and case)
    fake_api: FakeApi,
    fake_agent: FakeAgent,
    browser: TestClient,
    status: int,
    code: str,
    shown: str,
) -> None:
    """The page says what happened and keeps what the person wrote, to send again."""
    fake_agent.fail = (status, code)
    token = _signed_in(browser)
    conversation_id = _new_conversation(fake_api)

    response = browser.post(
        f"/chat/{conversation_id}",
        data={"csrf_token": token, "text": "My draft message"},
    )

    assert response.status_code == status
    assert shown in unescape(response.text)
    assert ">My draft message</textarea>" in response.text


def test_without_the_agent_service_the_page_says_so(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """With no agent service configured, the conversation shows but has no message box."""
    token = _signed_in(browser)
    conversation_id = _new_conversation(fake_api)

    page = browser.get(f"/chat/{conversation_id}").text
    sent = browser.post(f"/chat/{conversation_id}", data={"csrf_token": token, "text": "Hi"})

    assert "The assistant isn't open on this site yet" in unescape(page)
    assert "<textarea" not in page
    assert sent.status_code == 503


@pytest.mark.parametrize("text", ["", "   ", "x" * (MAX_MESSAGE_CHARACTERS + 1)])
def test_an_empty_or_long_message_isnt_sent(
    fake_api: FakeApi,
    fake_agent: FakeAgent,
    browser: TestClient,
    text: str,
) -> None:
    """The agent isn't called for a message it would refuse."""
    token = _signed_in(browser)
    conversation_id = _new_conversation(fake_api)

    response = browser.post(f"/chat/{conversation_id}", data={"csrf_token": token, "text": text})

    assert response.status_code == 400
    assert fake_agent.calls == []


def test_a_turn_needs_the_csrf_value(
    fake_api: FakeApi,
    fake_agent: FakeAgent,
    browser: TestClient,
) -> None:
    """A forged form can't send a message as the person."""
    _signed_in(browser)
    conversation_id = _new_conversation(fake_api)

    response = browser.post(f"/chat/{conversation_id}", data={"csrf_token": "x", "text": "Hi"})

    assert response.status_code == 403
    assert fake_agent.calls == []


def test_someone_elses_or_a_made_up_conversation_is_not_found(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """The API's 404 becomes a page; a value that isn't an ID never reaches the API."""
    _signed_in(browser)
    calls_before = len(fake_api.calls)

    missing = browser.get("/chat/6f1c2a52-0d6e-4b1f-8f43-2b7f3a0e5d11")
    made_up = browser.get("/chat/not-an-id")

    assert missing.status_code == 404
    assert made_up.status_code == 404
    paths = [p for _, p, _, _ in fake_api.calls[calls_before:]]
    assert paths == ["/conversations/6f1c2a52-0d6e-4b1f-8f43-2b7f3a0e5d11/messages"]


def test_signed_out_the_conversation_sends_you_to_sign_in(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Without a session, nothing is read."""
    response = browser.get("/chat/6f1c2a52-0d6e-4b1f-8f43-2b7f3a0e5d11")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert fake_api.calls == []


def test_the_agent_refusing_its_token_doesnt_sign_you_out(
    fake_api: FakeApi,
    fake_agent: FakeAgent,
    browser: TestClient,
) -> None:
    """A 401 from the agent side means the assistant is unavailable, not a bad session."""
    fake_agent.fail = (401, "unauthorized")
    token = _signed_in(browser)
    conversation_id = _new_conversation(fake_api)

    response = browser.post(f"/chat/{conversation_id}", data={"csrf_token": token, "text": "Hi"})

    assert response.status_code == 503
    assert UNAVAILABLE in unescape(response.text)
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert browser.cookies.get(SESSION_COOKIE) == SESSION_TOKEN


def test_the_api_refusing_the_session_signs_you_out(
    fake_api: FakeApi,
    fake_agent: FakeAgent,
    browser: TestClient,
) -> None:
    """A 401 when making the turn's token is the session itself: sign in again."""
    fake_api.fail["POST /auth/agent-token"] = (401, "unauthorized")
    token = _signed_in(browser)
    conversation_id = _new_conversation(fake_api)

    response = browser.post(f"/chat/{conversation_id}", data={"csrf_token": token, "text": "Hi"})

    assert response.status_code == 401
    assert SESSION_COOKIE in response.headers.get("set-cookie", "")
    assert fake_agent.calls == []
