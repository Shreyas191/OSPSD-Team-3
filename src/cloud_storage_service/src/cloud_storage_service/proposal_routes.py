"""HTTP routes for proposals: create, read, approve, reject, execute (issues #89, #90).

``POST /proposals/{id}/changes/{index}/resolve`` settles an ``uncertain`` change once the
person has checked the provider: it happened (``completed``, with the created record's ID
for a new event or card) or it didn't (``failed``, so the next run does it).

Every route acts as the user ``identity.get_current_user`` returns, and only on that user's
proposals: another user's proposal is 404. Errors use the shared error body.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from course_autopilot_store.models import ChangeOutcome, Proposal
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloud_storage_service import execution, proposals
from cloud_storage_service.connections import UserCalendar, UserIssues, UserStorage
from cloud_storage_service.errors import ErrorResponse, SharedErrorRoute, error_response
from cloud_storage_service.identity import CurrentUser, Person, RequiredDatabase

router = APIRouter(prefix="/proposals", tags=["proposals"], route_class=SharedErrorRoute)


class OutcomeResponse(BaseModel):
    """What happened to one change."""

    change_index: int
    status: str
    detail: str


class ProposalResponse(BaseModel):
    """A proposal, its exact content, and what has happened to each change so far."""

    id: uuid.UUID
    status: str
    changes: list[dict[str, Any]]
    changes_hash: str
    created_at: datetime
    decided_at: datetime | None
    outcomes: list[OutcomeResponse]


class ApproveRequest(BaseModel):
    """The hash of the content the person reviewed."""

    model_config = ConfigDict(extra="forbid")

    changes_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResolveRequest(BaseModel):
    """What the person found when they checked an uncertain change."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    record_id: str | None = Field(default=None, min_length=1, max_length=255)


ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse, "description": text}
    for code, text in {
        400: "A change is invalid, or targets a record this account didn't create",
        401: "Not signed in",
        404: "No such proposal for this account",
        409: "The proposal isn't in a state that allows this, or its content changed",
        503: "No database is configured, or running changes is paused (EXECUTION=disabled)",
    }.items()
}


def _response(session: Session, proposal: Proposal) -> ProposalResponse:
    outcomes = session.scalars(
        select(ChangeOutcome)
        .where(ChangeOutcome.proposal_id == proposal.id)
        .order_by(ChangeOutcome.change_index),
    )
    return ProposalResponse(
        id=proposal.id,
        status=proposal.status,
        changes=proposal.changes,
        changes_hash=proposal.changes_hash,
        created_at=proposal.created_at,
        decided_at=proposal.decided_at,
        outcomes=[
            OutcomeResponse(change_index=o.change_index, status=o.status, detail=o.detail)
            for o in outcomes
        ],
    )


def _error(e: proposals.ProposalError) -> JSONResponse:
    return error_response(e.status, e.code, e.message)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
    response_model=ProposalResponse,
)
def create_proposal(
    body: proposals.ProposalCreate,
    user: CurrentUser,
    database: RequiredDatabase,
) -> ProposalResponse | JSONResponse:
    """Store a pending change set. Nothing changes in any provider."""
    try:
        with database.session() as session:
            return _response(session, proposals.create(session, user, body))
    except proposals.ProposalError as e:
        return _error(e)


@router.get("/{proposal_id}", responses=ERRORS, response_model=ProposalResponse)
def get_proposal(
    proposal_id: uuid.UUID,
    user: CurrentUser,
    database: RequiredDatabase,
) -> ProposalResponse | JSONResponse:
    """Return one of this user's proposals with its outcomes."""
    try:
        with database.session() as session:
            return _response(session, proposals.get_owned(session, user, proposal_id))
    except proposals.ProposalError as e:
        return _error(e)


@router.post("/{proposal_id}/approve", responses=ERRORS, response_model=ProposalResponse)
def approve_proposal(
    proposal_id: uuid.UUID,
    body: ApproveRequest,
    person: Person,
    database: RequiredDatabase,
) -> ProposalResponse | JSONResponse:
    """Approve exactly the content with this hash."""
    try:
        with database.session() as session:
            return _response(
                session,
                proposals.approve(session, person.user_id, proposal_id, body.changes_hash),
            )
    except proposals.ProposalError as e:
        return _error(e)


@router.post("/{proposal_id}/reject", responses=ERRORS, response_model=ProposalResponse)
def reject_proposal(
    proposal_id: uuid.UUID,
    person: Person,
    database: RequiredDatabase,
) -> ProposalResponse | JSONResponse:
    """Reject a pending proposal."""
    try:
        with database.session() as session:
            return _response(session, proposals.reject(session, person.user_id, proposal_id))
    except proposals.ProposalError as e:
        return _error(e)


@router.post("/{proposal_id}/execute", responses=ERRORS, response_model=ProposalResponse)
def execute_proposal(  # noqa: PLR0913 (each argument is a FastAPI dependency)
    proposal_id: uuid.UUID,
    person: Person,
    database: RequiredDatabase,
    storage: UserStorage,
    issues: UserIssues,
    calendar: UserCalendar,
) -> ProposalResponse | JSONResponse:
    """Run an approved proposal, or retry a failed one; return every change's outcome."""
    if not execution.execution_enabled():
        return error_response(
            503,
            "execution_disabled",
            "Running changes is paused on this service; your approval is kept, try again later",
        )
    try:
        execution.execute(
            database,
            person.user_id,
            proposal_id,
            execution.Verticals(storage, issues, calendar),
        )
        with database.session() as session:
            return _response(session, proposals.get_owned(session, person.user_id, proposal_id))
    except proposals.ProposalError as e:
        return _error(e)


@router.post(
    "/{proposal_id}/changes/{change_index}/resolve",
    responses=ERRORS,
    response_model=ProposalResponse,
)
def resolve_change(  # noqa: PLR0913 (each argument is a FastAPI dependency)
    proposal_id: uuid.UUID,
    change_index: int,
    body: ResolveRequest,
    person: Person,
    database: RequiredDatabase,
    storage: UserStorage,
    issues: UserIssues,
    calendar: UserCalendar,
) -> ProposalResponse | JSONResponse:
    """Settle an uncertain change after checking it; return the proposal."""
    try:
        execution.resolve(
            database,
            person.user_id,
            proposal_id,
            change_index,
            execution.Resolution(body.status, body.record_id),
            execution.Verticals(storage, issues, calendar),
        )
        with database.session() as session:
            return _response(session, proposals.get_owned(session, person.user_id, proposal_id))
    except proposals.ProposalError as e:
        return _error(e)
