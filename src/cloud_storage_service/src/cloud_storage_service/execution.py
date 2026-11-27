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

A run that stops partway (a redeploy, a crash, a timeout) can't leave the proposal stuck.
Each change is saved as ``started`` just before its write, and the run updates the
proposal's ``heartbeat_at`` as it goes. Once a run has been silent for ``STALE_AFTER``,
running the proposal again takes it over, and a ``started`` change becomes ``uncertain``.

A run that was frozen rather than stopped (on Cloud Run, a request that outlives its timeout
can sit with no CPU, then carry on) must not act after it was taken over. Each run claims the
proposal with its own ``run_id``. Before each write, and for its final update, it checks the
proposal still names it; if not, it stops, so it never repeats a change the new run did. A
write it already had in flight is left as the new run recorded it (``uncertain``), and the
stopped run logs what that write did, for the person to settle.

The person settles an uncertain change after checking the provider (:func:`resolve`): it
happened (for a created event or card, they give its ID, which is read back before it is
recorded as ours) or it didn't (the next run does it). Once every change is completed, the
proposal is ``done``.

Completed changes stay: the person approved each one. Nothing is undone automatically.
Each run ends with one ``proposal_execution`` log line giving its final status and the count
of completed, failed and uncertain changes.
"""

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from calendar_contract import CalendarError, CalendarService, CreateEvent, ErrorCode, ReplaceEvent
from cloud_storage_api import DocumentStorage, StorageError
from cloud_storage_api.errors import ConflictError, ProviderError
from course_autopilot_store import Database
from course_autopilot_store.models import Approval, ChangeOutcome, ExternalRecord, Proposal
from issue_tracker_contract import IssueProvider
from issue_tracker_contract.errors import ErrorCode as IssueErrorCode
from issue_tracker_contract.errors import ServiceError
from issue_tracker_contract.models import IssueUpdate
from sqlalchemy import and_, or_, select, update

from cloud_storage_service.connections import ProviderNotReadyError
from cloud_storage_service.proposals import COURSE_ROOT, ProposalError, get_owned, owned_record

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

logger = logging.getLogger(__name__)

RUNNABLE = ("approved", "failed")
# The operator's switch (#102): EXECUTION=disabled stops every run before it claims anything,
# while reads, proposals and approvals keep working.
EXECUTION_ENV_VAR = "EXECUTION"


def execution_enabled() -> bool:
    """Whether proposals may run on this service (``EXECUTION`` isn't ``disabled``)."""
    return os.environ.get(EXECUTION_ENV_VAR, "").strip() != "disabled"


# A running proposal shows it's alive before each change. One change is one provider write
# and one read-back, each with its own timeout well under a minute, so a run silent this long
# has stopped (Cloud Run ends requests after 5 minutes).
STALE_AFTER = timedelta(minutes=10)
STARTED = "Running"
INTERRUPTED = (
    "The run stopped during this change, so it may or may not have happened; check, then "
    "mark it done or not done"
)


@dataclass(frozen=True)
class Resolution:
    """What the person found when they checked an uncertain change."""

    status: Literal["completed", "failed"]
    record_id: str | None = None


@dataclass(frozen=True)
class Verticals:
    """The three providers a proposal runs against: the person's own connections."""

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

    A proposal left ``executing`` by a run that stopped (a redeploy, a crash, a timeout) can
    run again once it has shown no sign of life for :data:`STALE_AFTER`. A change that run
    had ``started`` may have been written, so it becomes ``uncertain`` and isn't repeated.

    Raises:
        ProposalError: 404 if it isn't this user's; 409 if it isn't approved, its approval
            doesn't match its content, or another run holds it.

    """
    run_id = uuid.uuid4()
    changes, done = _claim(database, user_id, proposal_id, run_id)

    final = "done"
    for index, change in enumerate(changes):
        if done.get(index) in {"completed", "uncertain"}:
            if done[index] == "uncertain":
                final = "failed"
            continue
        outcome = _run_change(database, user_id, proposal_id, run_id, index, change, verticals)
        if outcome is None:  # taken over by a newer run, which decides from here
            return
        if outcome.status != "completed":
            final = "failed"
            break

    with database.session() as session:
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(Proposal)
                .where(Proposal.id == proposal_id, Proposal.run_id == run_id)
                .values(status=final, heartbeat_at=None, run_id=None),
            ),
        )
        if result.rowcount != 1:  # taken over after its last change: the new run decides
            _log_superseded(proposal_id, None, None)
            return
        _log_execution(session, proposal_id, final, len(changes))


def _claim(
    database: Database,
    user_id: uuid.UUID,
    proposal_id: uuid.UUID,
    run_id: uuid.UUID,
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """Claim the proposal for ``run_id``; return its changes and each change's saved status.

    Raises:
        ProposalError: as :func:`execute` documents.

    """
    now = datetime.now(UTC)
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
        # Claim the proposal atomically, so two concurrent runs can't both execute it. A run
        # silent for STALE_AFTER has stopped; its claim can be taken over.
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(Proposal)
                .where(
                    Proposal.id == proposal_id,
                    or_(
                        Proposal.status.in_(RUNNABLE),
                        and_(
                            Proposal.status == "executing",
                            Proposal.heartbeat_at < now - STALE_AFTER,
                        ),
                    ),
                )
                .values(status="executing", heartbeat_at=now, run_id=run_id)
                # Decided in the database alone: the loaded row's time may lack its zone.
                .execution_options(synchronize_session=False),
            ),
        )
        if result.rowcount != 1:
            raise ProposalError(
                409,
                "conflict",
                f"The proposal is {proposal.status}; it can't run now",
            )
        changes: list[dict[str, Any]] = list(proposal.changes)
        outcomes = list(
            session.scalars(select(ChangeOutcome).where(ChangeOutcome.proposal_id == proposal_id)),
        )
        for row in outcomes:
            if row.status == "started":  # left by a run that stopped mid-change
                row.status, row.detail, row.recorded_at = "uncertain", INTERRUPTED, now
        return changes, {o.change_index: o.status for o in outcomes}


def _run_change(  # noqa: PLR0913 (the run, the change and the providers)
    database: Database,
    user_id: uuid.UUID,
    proposal_id: uuid.UUID,
    run_id: uuid.UUID,
    index: int,
    change: dict[str, Any],
    verticals: Verticals,
) -> Outcome | None:
    """Run one change and save its outcome; ``None`` if a newer run took the proposal over."""
    with database.session() as session:
        # Fenced: a run that was taken over stops here, before it writes anything.
        if not _still_holds(session, proposal_id, run_id):
            _log_superseded(proposal_id, index, None)
            return None
        _record(session, user_id, proposal_id, index, change, Outcome("started", STARTED))
    with database.session() as session:
        outcome = _run(session, user_id, change, verticals)
        # Taken over during the write: the new run already made this change uncertain.
        if not _still_holds(session, proposal_id, run_id):
            _log_superseded(proposal_id, index, outcome)
            return None
        _record(session, user_id, proposal_id, index, change, outcome)
    return outcome


def _still_holds(session: Any, proposal_id: uuid.UUID, run_id: uuid.UUID) -> bool:  # noqa: ANN401
    """Update the heartbeat if this run still holds the proposal, and say whether it does.

    The check and the heartbeat are one conditional update, so no other run can take the
    proposal over between them.
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Proposal)
            .where(
                Proposal.id == proposal_id,
                Proposal.status == "executing",
                Proposal.run_id == run_id,
            )
            .values(heartbeat_at=datetime.now(UTC))
            .execution_options(synchronize_session=False),
        ),
    )
    return result.rowcount == 1


def _log_superseded(proposal_id: uuid.UUID, index: int | None, outcome: Outcome | None) -> None:
    """Log that a run stopped because another run took the proposal over.

    If a write was in flight, the line says what it did (including any record it created),
    so the person can settle the change the new run left ``uncertain``.
    """
    fields: dict[str, Any] = {"event": "proposal_run_superseded", "proposal_id": str(proposal_id)}
    if index is not None:
        fields["change_index"] = index
    if outcome is not None:
        fields["change_status"] = outcome.status
        fields["change_detail"] = outcome.detail
        if outcome.created:
            fields["created_kind"], fields["created_record_id"] = outcome.created
    logger.warning("Proposal run superseded by a newer run; stopping", extra=fields)


def _log_execution(session: Any, proposal_id: uuid.UUID, final: str, total: int) -> None:  # noqa: ANN401
    """Write one structured line per run, so dashboards and alerts see execution outcomes."""
    counts = dict.fromkeys(("completed", "failed", "uncertain"), 0)
    for status in session.scalars(
        select(ChangeOutcome.status).where(ChangeOutcome.proposal_id == proposal_id),
    ):
        counts[status] = counts.get(status, 0) + 1
    fields = {
        "event": "proposal_execution",
        "proposal_id": str(proposal_id),
        "final_status": final,
        "changes": total,
        **{f"changes_{name}": count for name, count in counts.items()},
    }
    level = logging.INFO if final == "done" else logging.WARNING
    logger.log(level, "Proposal execution %s", final, extra=fields)


# ----- The person settles an uncertain change -----

CREATES = {"create_event": "event", "create_issue": "issue"}


def resolve(  # noqa: PLR0913 (the proposal, the change, the person's answer and providers)
    database: Database,
    user_id: uuid.UUID,
    proposal_id: uuid.UUID,
    index: int,
    resolution: Resolution,
    verticals: Verticals,
) -> None:
    """Record what the person found when they checked an ``uncertain`` change.

    ``completed``: the change happened. For a created event or card, ``record_id`` names it;
    it is read back and must match the change (a card's title; an event's title, start and
    end) before it is recorded as ours, and it can't be a record already recorded for
    another change. A created document is found at its path. ``failed``: it didn't happen,
    so the next run does it again. Once every change is completed, the proposal is ``done``.

    The proposal's row is held while settling, so a run can't claim it in between: a run
    that already holds it makes this 409, and one that starts now waits until this ends.

    Raises:
        ProposalError: 404 if the proposal or change isn't this user's; 409 if the proposal
            is running or the change isn't uncertain; 400 if a created record's ID is
            missing, names a record that doesn't match the change, or is already recorded.

    """
    with database.session() as session:
        proposal = get_owned(session, user_id, proposal_id)
        # A no-op update that takes the row's lock until this transaction ends.
        held = cast(
            "CursorResult[Any]",
            session.execute(
                update(Proposal)
                .where(Proposal.id == proposal_id, Proposal.status != "executing")
                .values(status=Proposal.status)
                .execution_options(synchronize_session=False),
            ),
        )
        if held.rowcount != 1:
            raise ProposalError(409, "conflict", "The proposal is running; wait for it to end")
        changes: list[dict[str, Any]] = list(proposal.changes)
        if not 0 <= index < len(changes):
            raise ProposalError(404, "not_found", "The proposal has no such change")
        outcome = session.scalars(
            select(ChangeOutcome).where(
                ChangeOutcome.proposal_id == proposal_id,
                ChangeOutcome.change_index == index,
            ),
        ).first()
        if outcome is None or outcome.status != "uncertain":
            raise ProposalError(409, "conflict", "Only an uncertain change can be settled")
        change = changes[index]
        if resolution.status == "failed":
            settled = Outcome("failed", "You found it didn't happen; running again will do it")
        else:
            settled = _confirmed(session, user_id, change, resolution.record_id, verticals)
        _record(session, user_id, proposal_id, index, change, settled)
        statuses = list(
            session.scalars(
                select(ChangeOutcome.status).where(ChangeOutcome.proposal_id == proposal_id),
            ),
        )
        if len(statuses) == len(changes) and all(s == "completed" for s in statuses):
            # Only a failed proposal finishes here; a run's own final update decides otherwise.
            session.execute(
                update(Proposal)
                .where(Proposal.id == proposal_id, Proposal.status == "failed")
                .values(status="done")
                .execution_options(synchronize_session=False),
            )


def _confirmed(
    session: Any,  # noqa: ANN401
    user_id: uuid.UUID,
    change: dict[str, Any],
    record_id: str | None,
    v: Verticals,
) -> Outcome:
    """Return the outcome for a change the person says happened, after checking what we can."""
    confirmed = "You checked: it happened"
    op = change["op"]
    if op in CREATES:
        kind = CREATES[op]
        if not record_id:
            raise ProposalError(
                400,
                "invalid_request",
                f"Give the ID of the {kind} that was created, so it can be recorded",
            )
        if not _matches(kind, record_id, change, v):
            detail = "title, start and end" if kind == "event" else "title"
            raise ProposalError(
                400,
                "invalid_request",
                f"No {kind} with that ID and this change's {detail} was found",
            )
        if owned_record(session, user_id, kind, record_id) is not None:
            raise ProposalError(
                400,
                "invalid_request",
                f"That {kind} is already recorded for another change; give the one this "
                "change created",
            )
        return Outcome(
            "completed",
            f"{confirmed} ({kind} {record_id})",
            created=(kind, record_id),
        )
    if op == "write_document":
        path = f"{COURSE_ROOT}/{change['course']}/{change['name']}"
        ours = owned_record(session, user_id, "document", path) is not None
        return Outcome("completed", confirmed, created=None if ours else ("document", path))
    if op in {"delete_event", "delete_issue"}:
        kind = "event" if op == "delete_event" else "issue"
        target = change["event_id"] if kind == "event" else change["issue_id"]
        return Outcome("completed", confirmed, deleted=(kind, target))
    return Outcome("completed", confirmed)


def _matches(kind: str, record_id: str, change: dict[str, Any], v: Verticals) -> bool:
    """Whether the record exists and matches the change.

    An event must match its title, start and end; a card, its title.
    """
    try:
        if kind == "event":
            event = v.calendar.get(record_id)
            return bool(
                event.title == change["title"]
                and _same_time(event.start, change["start"])
                and _same_time(event.end, change["end"]),
            )
        return bool(v.issues.get_issue(record_id, fresh=True).title == change["title"])
    except (CalendarError, ServiceError):
        return False


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
    else:  # this change's ``started`` marker, or a failed outcome being retried
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
    except ProviderNotReadyError as e:
        # Not connected, refused, or needing setup: nothing was written.
        return Outcome("failed", e.message)
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
    if e.cause == "provider_status=429":
        return Outcome("failed", "Too many changes at once for Trello; try again in a minute")
    return Outcome("failed", "The issue tracker is unreachable; try again later")


def _storage_failure(e: StorageError) -> Outcome:
    logger.warning("Storage write failed (%s)", e.code)
    if isinstance(e, ConflictError):
        return Outcome(
            "failed",
            "A file with this name already exists in the course folder; rename or remove it",
        )
    return Outcome("failed", "Cloud storage is unreachable; try again later")
