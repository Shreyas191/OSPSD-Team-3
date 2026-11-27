"""Proposals: change sets the agent proposes and a person approves (issue #89).

A proposal is a list of **changes**, each exactly one provider write: create, update or
delete a calendar event or an issue, or write a course document. One write per change makes
every outcome exact (#90): a retry re-runs exactly the writes that didn't complete.

The content is fixed when the proposal is stored. ``changes_hash`` is the SHA-256 of its
canonical JSON; approving records the hash the person saw, and executing requires that the
approved hash still matches (DESIGN.md, decisions P1 and P2). Changing anything means a new
proposal.

Validation happens here, in code, before anything reaches a provider: every field against
the contracts' own models, and every update or delete against the records this user's
proposals created (``external_records``), so a proposal can never target someone else's
records or records we didn't create.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from calendar_contract import CreateEvent as CalendarCreateEvent
from course_autopilot_store.models import Approval, Conversation, ExternalRecord, Proposal
from issue_tracker_contract.models import IssueCreate
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

MAX_CHANGES = 100
MAX_DOCUMENT_CHARACTERS = 100_000
COURSE_ROOT = "/Course Autopilot"
MAX_COURSE_CHARACTERS = 100


class ProposalError(Exception):
    """A proposal request that can't be done. ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Record the status, the shared error code and a message safe to show."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def check_course(value: str) -> str:
    """Return ``value`` if it's a plain course name (one path segment); else raise ValueError."""
    if not value or len(value) > MAX_COURSE_CHARACTERS:
        msg = f"course must be 1 to {MAX_COURSE_CHARACTERS} characters"
        raise ValueError(msg)
    if value != value.strip() or value in {".", ".."} or "/" in value or "\\" in value:
        msg = "course must be a plain name, without slashes or surrounding spaces"
        raise ValueError(msg)
    return value


def _document_name(value: str) -> str:
    if "/" in value or "\\" in value or value.startswith(".") or not value.endswith(".md"):
        msg = "name must be a file name ending in .md, without slashes"
        raise ValueError(msg)
    return value


Course = Annotated[
    str,
    Field(min_length=1, max_length=MAX_COURSE_CHARACTERS),
    AfterValidator(check_course),
]
DocumentName = Annotated[str, Field(min_length=4, max_length=120), AfterValidator(_document_name)]
RecordId = Annotated[str, Field(min_length=1, max_length=512)]


class _Change(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    course: Course


class _EventFields(_Change):
    title: str
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _valid_for_the_calendar_contract(self) -> Self:
        # The calendar contract's own model checks the title and the time range.
        CalendarCreateEvent(
            title=self.title,
            start=self.start.isoformat(),
            end=self.end.isoformat(),
        )
        return self


class CreateEventChange(_EventFields):
    """Create a calendar event."""

    op: Literal["create_event"]


class UpdateEventChange(_EventFields):
    """Replace the title and times of an event this user's proposals created."""

    op: Literal["update_event"]
    event_id: RecordId


class DeleteEventChange(_Change):
    """Delete an event this user's proposals created."""

    op: Literal["delete_event"]
    event_id: RecordId


class _IssueFields(_Change):
    title: str

    @model_validator(mode="after")
    def _valid_for_the_issue_contract(self) -> Self:
        IssueCreate(title=self.title)
        return self


class CreateIssueChange(_IssueFields):
    """Create an issue (a Trello card)."""

    op: Literal["create_issue"]


class UpdateIssueChange(_IssueFields):
    """Rename an issue this user's proposals created."""

    op: Literal["update_issue"]
    issue_id: RecordId


class DeleteIssueChange(_Change):
    """Delete an issue this user's proposals created."""

    op: Literal["delete_issue"]
    issue_id: RecordId


class WriteDocumentChange(_Change):
    """Create a document in the course folder, or replace one this user's proposals created."""

    op: Literal["write_document"]
    name: DocumentName
    content: str = Field(max_length=MAX_DOCUMENT_CHARACTERS)

    @property
    def path(self) -> str:
        """Where the document lives in the user's storage."""
        return f"{COURSE_ROOT}/{self.course}/{self.name}"


Change = Annotated[
    CreateEventChange
    | UpdateEventChange
    | DeleteEventChange
    | CreateIssueChange
    | UpdateIssueChange
    | DeleteIssueChange
    | WriteDocumentChange,
    Field(discriminator="op"),
]


class ProposalCreate(BaseModel):
    """Body of ``POST /proposals``."""

    model_config = ConfigDict(extra="forbid")

    changes: list[Change] = Field(min_length=1, max_length=MAX_CHANGES)
    conversation_id: uuid.UUID | None = None


def canonical_changes(changes: list[Change]) -> list[dict[str, Any]]:
    """Return the changes as plain JSON values, in a stable form."""
    return [change.model_dump(mode="json") for change in changes]


def changes_hash(changes: list[dict[str, Any]]) -> str:
    """SHA-256 of the canonical JSON: sorted keys, no whitespace."""
    text = json.dumps(changes, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


_TARGET = {
    "update_event": ("event_id", "event"),
    "delete_event": ("event_id", "event"),
    "update_issue": ("issue_id", "issue"),
    "delete_issue": ("issue_id", "issue"),
}


def owned_record(
    session: Session,
    user_id: uuid.UUID,
    kind: str,
    record_id: str,
) -> ExternalRecord | None:
    """Return the live record of this kind that this user's proposals created, if any."""
    return session.scalars(
        select(ExternalRecord).where(
            ExternalRecord.user_id == user_id,
            ExternalRecord.kind == kind,
            ExternalRecord.provider_record_id == record_id,
            ExternalRecord.deleted_at.is_(None),
        ),
    ).first()


def check_targets(session: Session, user_id: uuid.UUID, changes: list[dict[str, Any]]) -> None:
    """Refuse any update or delete of a record this user's proposals didn't create."""
    for index, change in enumerate(changes):
        target = _TARGET.get(change["op"])
        if target and owned_record(session, user_id, target[1], change[target[0]]) is None:
            msg = f"Change {index} targets a {target[1]} this account didn't create"
            raise ProposalError(400, "invalid_request", msg)


def create(session: Session, user_id: uuid.UUID, body: ProposalCreate) -> Proposal:
    """Validate ownership and store a pending proposal."""
    changes = canonical_changes(body.changes)
    check_targets(session, user_id, changes)
    if body.conversation_id is not None:
        conversation = session.get(Conversation, body.conversation_id)
        if conversation is None or conversation.user_id != user_id:
            msg = "conversation_id isn't one of this account's conversations"
            raise ProposalError(400, "invalid_request", msg)
    proposal = Proposal(
        user_id=user_id,
        conversation_id=body.conversation_id,
        changes=changes,
        changes_hash=changes_hash(changes),
        status="pending",
    )
    session.add(proposal)
    session.flush()
    return proposal


def get_owned(session: Session, user_id: uuid.UUID, proposal_id: uuid.UUID) -> Proposal:
    """Return the proposal if this user owns it. Someone else's is 404, never 403."""
    proposal = session.get(Proposal, proposal_id)
    if proposal is None or proposal.user_id != user_id:
        raise ProposalError(404, "not_found", "Proposal not found")
    return proposal


def approve(
    session: Session,
    user_id: uuid.UUID,
    proposal_id: uuid.UUID,
    seen_hash: str,
) -> Proposal:
    """Record approval of the exact content the person saw."""
    proposal = get_owned(session, user_id, proposal_id)
    if proposal.status != "pending":
        raise ProposalError(409, "conflict", f"The proposal is {proposal.status}, not pending")
    if seen_hash != proposal.changes_hash:
        raise ProposalError(
            409,
            "conflict",
            "The proposal changed; review it again before approving",
        )
    session.add(Approval(proposal_id=proposal.id, user_id=user_id, changes_hash=seen_hash))
    proposal.status = "approved"
    proposal.decided_at = datetime.now(UTC)
    return proposal


def reject(session: Session, user_id: uuid.UUID, proposal_id: uuid.UUID) -> Proposal:
    """Reject a pending proposal; nothing in it will run."""
    proposal = get_owned(session, user_id, proposal_id)
    if proposal.status != "pending":
        raise ProposalError(409, "conflict", f"The proposal is {proposal.status}, not pending")
    proposal.status = "rejected"
    proposal.decided_at = datetime.now(UTC)
    return proposal
