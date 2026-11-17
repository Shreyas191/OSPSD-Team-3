"""``POST /workflows/bug-report``: the composed workflow over HTTP (see :mod:`workflow`).

Success is ``201`` with the three records. A failed step returns the service's error body
with four extra fields: ``step`` (which vertical failed), ``report_id``, ``undone`` (records
removed again) and ``left_behind`` (records that may remain). The status and ``code`` follow
the error model of the other routes:

| Failed step     | Cause                               | Status | ``code``                 |
|-----------------|-------------------------------------|--------|--------------------------|
| any (input)     | missing or invalid field            | 400    | ``invalid_request``      |
| ``storage``     | the storage contract's exceptions   | as on ``/files`` routes           |
| ``issue_tracker`` | any ``ServiceError``              | 502    | ``provider_error``       |
| ``calendar``    | ``provider_unavailable``            | 503    | ``provider_unavailable`` |
| ``calendar``    | any other ``CalendarError``         | 502    | ``provider_error``       |
"""

from typing import Annotated

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cloud_storage_service.dependencies import Calendar, IssueTracker, SharedStorage
from cloud_storage_service.errors import ErrorDetail, SharedErrorRoute
from cloud_storage_service.models import FileResponse
from cloud_storage_service.telemetry import record_failure
from cloud_storage_service.workflow import BugReport, StepFailed, run_bug_report

router = APIRouter(route_class=SharedErrorRoute, tags=["workflows"])

NonBlank = Field(pattern=r"\S")


class BugReportRequest(BaseModel):
    """A bug to record, track and schedule triage for."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, NonBlank, Field(min_length=1, max_length=120)] = Field(
        examples=["Login fails with 500 after password reset"],
    )
    description: Annotated[str, NonBlank, Field(min_length=1, max_length=10_000)] = Field(
        examples=["Steps: reset password, log in with the new one. Expected: dashboard."],
    )
    reporter: Annotated[str, NonBlank, Field(min_length=1, max_length=100)] = Field(
        examples=["victor"],
    )


class IssueResponse(BaseModel):
    """The issue created in the issue tracker."""

    id: str
    title: str


class EventResponse(BaseModel):
    """The triage event created in the calendar."""

    id: str
    title: str
    start: str
    end: str


class BugReportResponse(BaseModel):
    """The records one run created, one per vertical."""

    report_id: str = Field(examples=["3f2b9c0e5d7a4f1e8b6c2d9a0e4f7b13"])
    file: FileResponse
    issue: IssueResponse
    event: EventResponse


class WorkflowErrorDetail(ErrorDetail):
    """The shared error fields, plus which step failed and what state it left."""

    step: str = Field(examples=["calendar"])
    report_id: str
    undone: list[str] = Field(examples=[["issue 6afb8646dcaac3dc80f81bf2", "file /bug-reports/…"]])
    left_behind: list[str] = Field(examples=[[]])


class WorkflowErrorResponse(BaseModel):
    """The body of a failed workflow run."""

    error: WorkflowErrorDetail


_FAILURE = {"model": WorkflowErrorResponse}


@router.post(
    "/workflows/bug-report",
    status_code=status.HTTP_201_CREATED,
    response_model=BugReportResponse,
    responses={
        400: {"description": "Missing or invalid field"},
        409: {**_FAILURE, "description": "storage: the report file already exists"},
        502: {**_FAILURE, "description": "A provider failed; earlier steps were undone"},
        503: {**_FAILURE, "description": "A provider is unreachable; earlier steps were undone"},
    },
)
def create_bug_report(
    request: BugReportRequest,
    storage: SharedStorage,
    issues: IssueTracker,
    calendar: Calendar,
) -> BugReportResponse | JSONResponse:
    """Save a bug report, open an issue for it, and book a triage meeting.

    Steps run in a fixed order: storage, issue tracker, calendar. If one fails, the steps
    before it are undone and the error names the failed ``step``. A retry is safe: it starts
    over with a new ``report_id``.
    """
    try:
        result = run_bug_report(
            BugReport(request.title, request.description, request.reporter),
            storage=storage,
            issues=issues,
            calendar=calendar,
        )
    except StepFailed as failure:
        record_failure("create_bug_report", failure.status, failure.code, str(failure))
        detail = WorkflowErrorDetail(
            code=failure.code,
            message=failure.message,
            step=failure.step,
            report_id=failure.report_id,
            undone=failure.undone,
            left_behind=failure.left_behind,
        )
        return JSONResponse(
            WorkflowErrorResponse(error=detail).model_dump(),
            status_code=failure.status,
        )
    return BugReportResponse(
        report_id=result.report_id,
        file=FileResponse.from_info(result.file),
        issue=IssueResponse(id=result.issue.id, title=result.issue.title),
        event=EventResponse(**result.event.model_dump()),
    )
