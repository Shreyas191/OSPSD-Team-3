"""Executing an approved proposal (issue #90; DESIGN.md, decision P3).

Changes run in order, each one provider write through its shared contract. After each write
the record is read back, and the change is ``completed`` only if the read-back matches. Each
outcome is saved as it happens, so a crash mid-run loses nothing already done.

- **completed**: written and confirmed.
- **failed**: not written (the provider refused or was unreachable before writing).
- **uncertain**: may or may not have been written (a lost reply, or a read-back that
  differs). Never retried automatically: none of the contracts has an idempotency key, and
  each says an uncertain write must not be retried blindly.

Execution stops at the first change that isn't completed; later changes don't run. Running
the proposal again (allowed once it has ``failed``) re-runs only changes with no outcome
or a ``failed`` one, so a completed write never happens twice and an uncertain one waits for
the person.

Completed changes stay: the person approved each one. Nothing is undone automatically.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from calendar_contract import CalendarError, CalendarService, CreateEvent, ErrorCode, ReplaceEvent
from cloud_storage_api import DocumentStorage, StorageError
from cloud_storage_api.errors import ConflictError, ProviderError
from course_autopilot_store import Database
from course_autopilot_store.models import Approval, ChangeOutcome, ExternalRecord, Proposal
from issue_tracker_contract import IssueProvider
from issue_tracker_contract.errors import ErrorCode as IssueErrorCode
from issue_tracker_contract.errors import ServiceError
from issue_tracker_contract.models import IssueUpdate
from sqlalchemy import select, update

from cloud_storage_service.proposals import COURSE_ROOT, ProposalError, get_owned, owned_record

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

logger = logging.getLogger(__name__)

RUNNABLE = ("approved", "failed")


@dataclass(frozen=True)
class Verticals:
    """The three providers a proposal runs against (per user once #98-#101 land)."""

    storage: DocumentStorage
    issues: IssueProvider
    calendar: CalendarService


@dataclass(frozen=True)
class Outcome:
    """What one change did."""

    status: str  # completed | failed | uncertain
    detail: str
    created: tuple[str, str] | None = None  # (kind, provider record ID) of a new record
    deleted: tuple[str, str] | None = None  # (kind, provider record ID) of a removed record


class _Uncertain(Exception):  # noqa: N818 (an outcome, not an error condition)
    """The write may have happened; ``detail`` says what to check."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def execute(
    database: Database,
    user_id: uuid.UUID,
    proposal_id: uuid.UUID,
    verticals: Verticals,
) -> None:
    """Run an approved (or previously failed) proposal of this user's.

    Raises:
        ProposalError: 404 if it isn't this user's; 409 if it isn't approved, its approval
            doesn't match its content, or another run holds it.

    """
    with database.session() as session:
        proposal = get_owned(session, user_id, proposal_id)
        approval = session.scalars(
            select(Approval).where(Approval.proposal_id == proposal_id),
        ).first()
        if approval is None or approval.changes_hash != proposal.changes_hash:
            raise ProposalError(
                409,
                "conflict",
                "The proposal has no approval for its current content",
            )
        # Claim the proposal atomically, so two concurrent runs can't both execute it.
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(Proposal)
                .where(Proposal.id == proposal_id, Proposal.status.in_(RUNNABLE))
                .values(status="executing"),
            ),
        )
        claimed = result.rowcount
        if claimed != 1:
            raise ProposalError(
                409,
                "conflict",
                f"The proposal is {proposal.status}; it can't run now",
            )
        changes: list[dict[str, Any]] = list(proposal.changes)
        done = {
            o.change_index: o.status
            for o in session.scalars(
                select(ChangeOutcome).where(ChangeOutcome.proposal_id == proposal_id),
            )
        }

    final = "done"
    for index, change in enumerate(changes):
        if done.get(index) in {"completed", "uncertain"}:
            if done[index] == "uncertain":
                final = "failed"
            continue
        with database.session() as session:
            outcome = _run(session, user_id, change, verticals)
            _record(session, user_id, proposal_id, index, change, outcome)
        if outcome.status != "completed":
            final = "failed"
            break

    with database.session() as session:
        session.execute(update(Proposal).where(Proposal.id == proposal_id).values(status=final))


def _record(  # noqa: PLR0913 (the outcome needs its proposal, change and user)
    session: Any,  # noqa: ANN401 (a SQLAlchemy Session; Any keeps the signature short)
    user_id: uuid.UUID,
    proposal_id: uuid.UUID,
    index: int,
    change: dict[str, Any],
    outcome: Outcome,
) -> None:
    existing = session.scalars(
        select(ChangeOutcome).where(
            ChangeOutcome.proposal_id == proposal_id,
            ChangeOutcome.change_index == index,
        ),
    ).first()
    if existing is None:
        session.add(
            ChangeOutcome(
                proposal_id=proposal_id,
                change_index=index,
                status=outcome.status,
                detail=outcome.detail,
            ),
        )
    else:  # a retry of a failed change
        existing.status, existing.detail, existing.recorded_at = (
            outcome.status,
            outcome.detail,
            datetime.now(UTC),
        )
    if outcome.created:
        kind, record_id = outcome.created
        session.add(
            ExternalRecord(
                user_id=user_id,
                course_key=change["course"],
                kind=kind,
                provider_record_id=record_id,
                created_by_proposal_id=proposal_id,
            ),
        )
    if outcome.deleted:
        record = owned_record(session, user_id, *outcome.deleted)
        if record is not None:
            record.deleted_at = datetime.now(UTC)


def _run(session: Any, user_id: uuid.UUID, change: dict[str, Any], v: Verticals) -> Outcome:  # noqa: ANN401
    runner: Callable[[Any, uuid.UUID, dict[str, Any], Verticals], Outcome] = _RUNNERS[change["op"]]
    try:
        return runner(session, user_id, change, v)
    except _Uncertain as e:
        return Outcome("uncertain", e.detail)
    except CalendarError as e:
        return _calendar_failure(e, change)
    except ServiceError as e:
        return _issue_failure(e, change)
    except StorageError as e:
        return _storage_failure(e)


# ----- Runners: one provider write, then a read-back -----


def _not_ours(kind: str) -> Outcome:
    return Outcome(
        "failed",
        f"This {kind} was already removed or wasn't created by Course Autopilot",
    )


def _same_time(a: str, b: str) -> bool:
    return datetime.fromisoformat(a) == datetime.fromisoformat(b)


def _create_event(_s: Any, _u: uuid.UUID, c: dict[str, Any], v: Verticals) -> Outcome:  # noqa: ANN401
    event = v.calendar.create(CreateEvent(title=c["title"], start=c["start"], end=c["end"]))
    back = v.calendar.get(event.id)
    if back.title != c["title"] or not (
        _same_time(back.start, c["start"]) and _same_time(back.end, c["end"])
    ):
        msg = f"Event {event.id} was created but reads back differently; check it in your calendar"
        raise _Uncertain(msg)
    return Outcome("completed", f"Created event {event.id}", created=("event", event.id))


def _update_event(s: Any, u: uuid.UUID, c: dict[str, Any], v: Verticals) -> Outcome:  # noqa: ANN401
    if owned_record(s, u, "event", c["event_id"]) is None:
        return _not_ours("event")
    v.calendar.replace(
        c["event_id"],
        ReplaceEvent(title=c["title"], start=c["start"], end=c["end"]),
    )
    back = v.calendar.get(c["event_id"])
    if back.title != c["title"] or not (
        _same_time(back.start, c["start"]) and _same_time(back.end, c["end"])
    ):
        msg = f"Event {c['event_id']} reads back differently after updating; check your calendar"
        raise _Uncertain(msg)
    return Outcome("completed", f"Updated event {c['event_id']}")


def _delete_event(s: Any, u: uuid.UUID, c: dict[str, Any], v: Verticals) -> Outcome:  # noqa: ANN401
    if owned_record(s, u, "event", c["event_id"]) is None:
        return _not_ours("event")
    v.calendar.delete(c["event_id"])
    try:
        v.calendar.get(c["event_id"])
    except CalendarError as e:
        if e.code == ErrorCode.NOT_FOUND:
            return Outcome(
                "completed",
                f"Deleted event {c['event_id']}",
                deleted=("event", c["event_id"]),
            )
        raise
    msg = f"Event {c['event_id']} still exists after the delete; check your calendar"
    raise _Uncertain(msg)


def _create_issue(_s: Any, _u: uuid.UUID, c: dict[str, Any], v: Verticals) -> Outcome:  # noqa: ANN401
    issue = v.issues.create_issue(c["title"])
    back = v.issues.get_issue(issue.id, fresh=True)
    if back.title != c["title"]:
        msg = f"Card {issue.id} was created but reads back differently; check your board"
        raise _Uncertain(msg)
    return Outcome("completed", f"Created card {issue.id}", created=("issue", issue.id))


def _update_issue(s: Any, u: uuid.UUID, c: dict[str, Any], v: Verticals) -> Outcome:  # noqa: ANN401
    if owned_record(s, u, "issue", c["issue_id"]) is None:
        return _not_ours("card")
    v.issues.update_issue(c["issue_id"], IssueUpdate(title=c["title"]))
    if v.issues.get_issue(c["issue_id"], fresh=True).title != c["title"]:
        msg = f"Card {c['issue_id']} reads back differently after the update; check your board"
        raise _Uncertain(msg)
    return Outcome("completed", f"Renamed card {c['issue_id']}")


def _delete_issue(s: Any, u: uuid.UUID, c: dict[str, Any], v: Verticals) -> Outcome:  # noqa: ANN401
    if owned_record(s, u, "issue", c["issue_id"]) is None:
        return _not_ours("card")
    v.issues.delete_issue(c["issue_id"])
    try:
        v.issues.get_issue(c["issue_id"], fresh=True)
    except ServiceError as e:
        if e.code == IssueErrorCode.NOT_FOUND:
            return Outcome(
                "completed",
                f"Deleted card {c['issue_id']}",
                deleted=("issue", c["issue_id"]),
            )
        raise
    msg = f"Card {c['issue_id']} still exists after the delete; check your board"
    raise _Uncertain(msg)


def _write_document(s: Any, u: uuid.UUID, c: dict[str, Any], v: Verticals) -> Outcome:  # noqa: ANN401
    path = f"{COURSE_ROOT}/{c['course']}/{c['name']}"
    content = c["content"].encode()
    ours = owned_record(s, u, "document", path) is not None
    try:
        if ours:
            v.storage.replace(path, content)
        else:
            v.storage.create(path, content)
    except ProviderError:
        msg = f"The upload of {path} may or may not have finished; check the file"
        raise _Uncertain(msg) from None
    # Read back the metadata, not the whole file: the size must match what we wrote.
    if v.storage.get_metadata(path).size != len(content):
        msg = f"{path} reads back with a different size; check the file"
        raise _Uncertain(msg)
    if ours:
        return Outcome("completed", f"Updated {path}")
    return Outcome("completed", f"Created {path}", created=("document", path))


_RUNNERS: dict[str, Callable[[Any, uuid.UUID, dict[str, Any], Verticals], Outcome]] = {
    "create_event": _create_event,
    "update_event": _update_event,
    "delete_event": _delete_event,
    "create_issue": _create_issue,
    "update_issue": _update_issue,
    "delete_issue": _delete_issue,
    "write_document": _write_document,
}


# ----- Provider errors as outcomes. Fixed messages: provider text never reaches the person. -----


def _calendar_failure(e: CalendarError, change: dict[str, Any]) -> Outcome:
    logger.warning("Calendar %s failed (%s)", change["op"], e.code)
    if e.code == ErrorCode.PROVIDER_ERROR and not e.retryable:
        return Outcome(
            "uncertain",
            "The calendar didn't confirm this change; check your calendar before retrying",
        )
    if e.code == ErrorCode.NOT_FOUND:
        return Outcome("failed", "The event no longer exists in your calendar")
    if e.code == ErrorCode.ACCESS_DENIED:
        return Outcome("failed", "The calendar refused access; reconnect Google Calendar")
    return Outcome("failed", "The calendar is unreachable; try again later")


def _issue_failure(e: ServiceError, change: dict[str, Any]) -> Outcome:
    logger.warning("Issue tracker %s failed (%s)", change["op"], e.code)
    if e.may_have_applied:
        return Outcome(
            "uncertain",
            "The issue tracker didn't confirm this change; check your board before retrying",
        )
    if e.code == IssueErrorCode.NOT_FOUND:
        return Outcome("failed", "The card no longer exists on your board")
    if e.code == IssueErrorCode.ACCESS_DENIED:
        return Outcome("failed", "The issue tracker refused access; reconnect Trello")
    return Outcome("failed", "The issue tracker is unreachable; try again later")


def _storage_failure(e: StorageError) -> Outcome:
    logger.warning("Storage write failed (%s)", e.code)
    if isinstance(e, ConflictError):
        return Outcome(
            "failed",
            "A file with this name already exists in the course folder; rename or remove it",
        )
    return Outcome("failed", "Cloud storage is unreachable; try again later")
