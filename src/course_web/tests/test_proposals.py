"""Reviewing, approving, running and settling a proposal, against a fake API service."""

from html import unescape

from fastapi.testclient import TestClient

from course_web.app import NOTICES, SESSION_COOKIE
from course_web.views import when

from .conftest import CARD, EVENT, PLAN, SESSION_TOKEN, FakeApi, changes_hash, csrf


def _signed_in(browser: TestClient) -> str:
    browser.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
    return csrf(browser)


def _calls(fake_api: FakeApi, proposal_id: str) -> list[str]:
    prefix = f"/proposals/{proposal_id}"
    return [
        f"{m} {p.removeprefix(prefix) or '/'}"
        for m, p, _, _ in fake_api.calls
        if p.startswith(prefix)
    ]


def test_the_page_shows_every_change_as_stored(fake_api: FakeApi, browser: TestClient) -> None:
    """The event's time in its own zone, the card, and the document's full text."""
    proposal_id = fake_api.add_proposal([EVENT, CARD, PLAN])
    _signed_in(browser)

    page = unescape(browser.get(f"/proposals/{proposal_id}").text)

    assert "Add a calendar event" in page
    assert "Thu Dec 3, 2026, 11:00 PM to 11:59 PM (UTC-05:00)" in page
    assert "Add a Trello card" in page
    assert "- Homework 1: Dec 3 (10%)" in page
    assert page.count('name="keep"') == 3
    assert f'name="changes_hash" value="{changes_hash([EVENT, CARD, PLAN])}"' in page


def test_approving_all_approves_the_reviewed_hash_then_runs(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Approve names the hash the page showed, then the changes are made and shown done."""
    proposal_id = fake_api.add_proposal([EVENT, CARD])
    token = _signed_in(browser)

    response = browser.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token, "changes_hash": changes_hash([EVENT, CARD]), "keep": ["0", "1"]},
    )
    page = unescape(browser.get(f"/proposals/{proposal_id}").text)

    assert response.status_code == 303
    assert _calls(fake_api, proposal_id)[:3] == ["GET /", "POST /approve", "POST /execute"]
    assert fake_api.proposals[proposal_id]["status"] == "done"
    assert "All changes are made." in page
    assert page.count("Done") >= 2


def test_changes_that_differ_from_the_reviewed_ones_arent_approved(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """A form from an older view of the proposal approves nothing."""
    proposal_id = fake_api.add_proposal([EVENT])
    token = _signed_in(browser)

    response = browser.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token, "changes_hash": changes_hash([CARD]), "keep": ["0"]},
    )

    assert response.status_code == 409
    assert fake_api.proposals[proposal_id]["status"] == "pending"
    assert "POST /approve" not in _calls(fake_api, proposal_id)


def test_unchecking_a_change_makes_a_new_proposal_to_review(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """Nothing is approved: the old proposal is rejected, the kept changes are shown again."""
    proposal_id = fake_api.add_proposal([EVENT, CARD, PLAN])
    token = _signed_in(browser)

    response = browser.post(
        f"/proposals/{proposal_id}/approve",
        data={
            "csrf_token": token,
            "changes_hash": changes_hash([EVENT, CARD, PLAN]),
            "keep": ["0", "2"],
        },
    )
    [new_id] = set(fake_api.proposals) - {proposal_id}
    page = unescape(browser.get(response.headers["location"]).text)

    assert response.headers["location"] == f"/proposals/{new_id}?notice=kept"
    assert fake_api.proposals[proposal_id]["status"] == "rejected"
    assert fake_api.proposals[new_id]["changes"] == [EVENT, PLAN]
    assert fake_api.proposals[new_id]["status"] == "pending"
    assert NOTICES["kept"] in page
    assert "Approve and make these changes" in page


def test_unchecking_every_change_rejects_the_proposal(
    fake_api: FakeApi,
    browser: TestClient,
) -> None:
    """With nothing kept, the proposal is rejected and no new one is made."""
    proposal_id = fake_api.add_proposal([EVENT])
    token = _signed_in(browser)

    response = browser.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token, "changes_hash": changes_hash([EVENT])},
    )

    assert response.headers["location"] == f"/proposals/{proposal_id}?notice=none_kept"
    assert fake_api.proposals[proposal_id]["status"] == "rejected"
    assert len(fake_api.proposals) == 1


def test_rejecting_changes_nothing(fake_api: FakeApi, browser: TestClient) -> None:
    """Reject all records the decision and runs nothing."""
    proposal_id = fake_api.add_proposal([EVENT])
    token = _signed_in(browser)

    browser.post(f"/proposals/{proposal_id}/reject", data={"csrf_token": token})
    page = unescape(browser.get(f"/proposals/{proposal_id}").text)

    assert fake_api.proposals[proposal_id]["status"] == "rejected"
    assert "You rejected these changes. Nothing was changed." in page
    assert "POST /execute" not in _calls(fake_api, proposal_id)


def test_approve_needs_the_csrf_value(fake_api: FakeApi, browser: TestClient) -> None:
    """A forged approval from another site is refused before the API is called."""
    proposal_id = fake_api.add_proposal([EVENT])
    _signed_in(browser)

    response = browser.post(
        f"/proposals/{proposal_id}/approve",
        data={"changes_hash": changes_hash([EVENT]), "keep": ["0"]},
    )

    assert response.status_code == 403
    assert fake_api.proposals[proposal_id]["status"] == "pending"


def test_paused_execution_keeps_the_approval(fake_api: FakeApi, browser: TestClient) -> None:
    """With execution switched off, the approval stays and the page says to try later."""
    proposal_id = fake_api.add_proposal([EVENT])
    fake_api.fail[f"POST /proposals/{proposal_id}/execute"] = (503, "execution_disabled")
    token = _signed_in(browser)

    response = browser.post(
        f"/proposals/{proposal_id}/approve",
        data={"csrf_token": token, "changes_hash": changes_hash([EVENT]), "keep": ["0"]},
    )
    page = unescape(browser.get(response.headers["location"]).text)

    assert response.headers["location"] == f"/proposals/{proposal_id}?notice=paused"
    assert fake_api.proposals[proposal_id]["status"] == "approved"
    assert NOTICES["paused"] in page
    assert 'action="/proposals/' + proposal_id + '/execute"' in page  # run it later


def test_an_uncertain_change_can_be_settled(fake_api: FakeApi, browser: TestClient) -> None:
    """An uncertain card shows a form; "it happened" sends its ID to the API."""
    proposal_id = fake_api.add_proposal([EVENT, CARD], status="approved")
    fake_api.execute_as = {1: "uncertain"}
    token = _signed_in(browser)
    browser.post(f"/proposals/{proposal_id}/execute", data={"csrf_token": token})

    page = unescape(browser.get(f"/proposals/{proposal_id}").text)
    settled = browser.post(
        f"/proposals/{proposal_id}/changes/1/resolve",
        data={"csrf_token": token, "happened": "yes", "record_id": " card-42 "},
    )

    assert "Not sure it happened" in page
    assert f'action="/proposals/{proposal_id}/changes/1/resolve"' in page
    assert "Check your Trello board" in page
    assert settled.headers["location"] == f"/proposals/{proposal_id}?notice=settled"
    resolve_body = next(b for _, p, _, b in fake_api.calls if p.endswith("/changes/1/resolve"))
    assert resolve_body == {"status": "completed", "record_id": "card-42"}


def test_settling_needs_an_answer(fake_api: FakeApi, browser: TestClient) -> None:
    """Without "it happened" or "it didn't", nothing is sent."""
    proposal_id = fake_api.add_proposal([CARD], status="failed")
    token = _signed_in(browser)

    response = browser.post(
        f"/proposals/{proposal_id}/changes/0/resolve",
        data={"csrf_token": token, "happened": "maybe"},
    )

    assert response.status_code == 400
    assert not any(p.endswith("/resolve") for _, p, _, _ in fake_api.calls)


def test_a_notice_is_chosen_by_key_never_echoed(fake_api: FakeApi, browser: TestClient) -> None:
    """Text in the URL never reaches the page: only the notices we wrote can show."""
    proposal_id = fake_api.add_proposal([EVENT])
    _signed_in(browser)

    page = browser.get(f"/proposals/{proposal_id}", params={"notice": "Click evil.test"}).text

    assert "evil.test" not in page


def test_someone_elses_proposal_is_not_found(fake_api: FakeApi, browser: TestClient) -> None:
    """The API's 404 becomes a page; a value that isn't an ID never reaches the API."""
    _signed_in(browser)
    calls_before = len(fake_api.calls)

    missing = browser.get("/proposals/6f1c2a52-0d6e-4b1f-8f43-2b7f3a0e5d11")
    made_up = browser.post("/proposals/not-an-id/approve", data={"csrf_token": csrf(browser)})

    assert missing.status_code == 404
    assert made_up.status_code == 404
    paths = [p for _, p, _, _ in fake_api.calls[calls_before:] if p.startswith("/proposals")]
    assert paths == ["/proposals/6f1c2a52-0d6e-4b1f-8f43-2b7f3a0e5d11"]


def test_times_read_in_the_changes_own_zone() -> None:
    """A range across days names both days; a value that isn't a time is shown as written."""
    assert when("2026-12-03T23:00:00-05:00", "2026-12-04T01:00:00-05:00") == (
        "Thu Dec 3, 2026, 11:00 PM to Fri Dec 4, 1:00 AM (UTC-05:00)"
    )
    assert when("soon", "later") == "soon to later"
