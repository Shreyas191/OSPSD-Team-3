"""How a proposal's changes and outcomes read on the page, in the student's terms.

Pure functions, no I/O: the page shows exactly the content the API stored, so what the
student approves is what they read (DESIGN.md, P2). Every value is shown as text and escaped
by the template; nothing here produces markup.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from course_web.backend import Outcome

ACTIONS = {
    "create_event": "Add a calendar event",
    "update_event": "Change a calendar event",
    "delete_event": "Remove a calendar event",
    "create_issue": "Add a Trello card",
    "update_issue": "Rename a Trello card",
    "delete_issue": "Remove a Trello card",
    "write_document": "Write a document in your Dropbox",
}

# A created event or card is recorded as ours only with its ID (the API reads it back).
CREATES_A_RECORD = {"create_event", "create_issue"}

OUTCOMES = {
    "completed": "Done",
    "failed": "Failed",
    "uncertain": "Not sure it happened",
    "started": "In progress",
}

# What the whole proposal's status means for the person, and what they can do next.
STATUSES = {
    "pending": "Waiting for your approval. Nothing has been changed yet.",
    "approved": "Approved. The changes haven't been made yet.",
    "rejected": "You rejected these changes. Nothing was changed.",
    "executing": "The changes are being made. Reload this page in a moment. If it still says "
    "this after a few minutes, the run stopped: run them again, and changes already made "
    "aren't repeated.",
    "failed": "Some changes weren't made. Nothing is undone: changes marked Done stay.",
    "done": "All changes are made.",
}


@dataclass(frozen=True)
class ChangeView:
    """One change as the page shows it."""

    index: int
    action: str
    course: str
    title: str
    when: str
    document: str
    content: str
    record_id: str
    creates_record: bool
    outcome: str
    outcome_status: str
    detail: str


def when(start: str, end: str) -> str:
    """Return an event's time range, in the time zone the change states.

    For example ``Thu Dec 3, 2026, 11:00 PM to 11:59 PM (UTC-05:00)``. A value that isn't a
    time is shown as written, so the page never hides what was proposed.
    """
    try:
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return f"{start} to {end}"
    if first.tzinfo and last.tzinfo:
        # The API keeps each time's own offset; show both in the start's, which is the label.
        last = last.astimezone(first.tzinfo)
    offset = first.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else ""
    day = f"{first:%a %b} {first.day}, {first.year}"
    start_time = f"{first.hour % 12 or 12}:{first:%M %p}"
    end_time = f"{last.hour % 12 or 12}:{last:%M %p}"
    if last.date() != first.date():
        end_time = f"{last:%a %b} {last.day}, {end_time}"
    return (
        f"{day}, {start_time} to {end_time} ({zone})"
        if zone
        else f"{day}, {start_time} to {end_time}"
    )


def change_views(changes: list[dict[str, Any]], outcomes: dict[int, Outcome]) -> list[ChangeView]:
    """Return each change with what happened to it so far."""
    views = []
    for index, change in enumerate(changes):
        op = str(change.get("op", ""))
        outcome = outcomes.get(index)
        is_event = op in {"create_event", "update_event"}
        views.append(
            ChangeView(
                index=index,
                action=ACTIONS.get(op, op),
                course=str(change.get("course", "")),
                title=str(change.get("title", "")),
                when=when(str(change["start"]), str(change["end"])) if is_event else "",
                document=str(change.get("name", "")) if op == "write_document" else "",
                content=str(change.get("content", "")) if op == "write_document" else "",
                record_id=str(change.get("event_id") or change.get("issue_id") or ""),
                creates_record=op in CREATES_A_RECORD,
                outcome=OUTCOMES.get(outcome.status, outcome.status) if outcome else "",
                outcome_status=outcome.status if outcome else "",
                detail=outcome.detail if outcome else "",
            ),
        )
    return views
