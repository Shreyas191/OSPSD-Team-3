"""Read routes for a user's courses: what the agent's read-only tools call (issues #88, #91).

- ``GET /courses``: the courses this user has, from the folders under ``/Course Autopilot``
  and the records our proposals created.
- ``GET /courses/{course}/documents``: the files in one course's folder.
- ``GET /courses/{course}/documents/{name}``: the text of one of them (PDF, Markdown or plain
  text), capped at 20,000 characters.
- ``GET /courses/{course}/plan``: the live records our proposals created for the course, read
  back from each provider.

Every read stays inside ``/Course Autopilot/<course>/`` (threat T11): the course and the file
name are single path segments, checked here before any storage call, so ``..``, slashes and
absolute paths are 400 and nothing is read. Every route acts as the signed-in user only.
Nothing here writes to a provider.
"""

import io
import logging
from typing import Any

from calendar_contract import CalendarError
from cloud_storage_api import StorageError
from cloud_storage_api.errors import NotFoundError, PermissionDeniedError, ProviderUnavailableError
from course_autopilot_store.models import ExternalRecord
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from issue_tracker_contract.errors import ServiceError
from pydantic import BaseModel
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from sqlalchemy import func, select

from cloud_storage_service import proposals
from cloud_storage_service.dependencies import Calendar, DocumentStore, IssueTracker
from cloud_storage_service.errors import ErrorResponse, SharedErrorRoute, error_response
from cloud_storage_service.identity import CurrentUser, RequiredDatabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/courses", tags=["courses"], route_class=SharedErrorRoute)

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARACTERS = 20_000
READABLE_SUFFIXES = (".pdf", ".md", ".txt")

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse, "description": text}
    for code, text in {
        400: "The course or file name isn't a single plain name",
        401: "Not signed in",
        404: "No such course folder or file",
        413: "The file is larger than 10 MB",
        422: "The file can't be read as text (an unsupported type, or a scanned PDF)",
        502: "The storage provider failed",
        503: "The storage provider is unreachable, or no database is configured",
    }.items()
}


class CourseSummary(BaseModel):
    """One course and how many live records our proposals created for it."""

    course: str
    has_folder: bool
    events: int
    issues: int
    documents: int


class DocumentEntry(BaseModel):
    """A file in a course folder."""

    name: str
    size: int | None
    modified_at: str | None
    readable: bool


class DocumentText(BaseModel):
    """The text of one document. ``truncated`` is true when it was cut to the cap."""

    course: str
    name: str
    text: str
    characters: int
    truncated: bool


class PlanItem(BaseModel):
    """A record our proposals created, as its provider reports it now."""

    kind: str  # event | issue | document
    id: str
    title: str
    start: str | None = None
    end: str | None = None
    available: bool = True


class CoursePlan(BaseModel):
    """The live records of one course."""

    course: str
    items: list[PlanItem]


class _BadName(Exception):  # noqa: N818 (an outcome of validation)
    pass


def _course(value: str) -> str:
    try:
        proposals.check_course(value)
    except ValueError as e:
        raise _BadName(str(e)) from None
    return value


def _document_name(value: str) -> str:
    if (
        not value
        or len(value) > 255  # noqa: PLR2004 (a file name's usual limit)
        or "/" in value
        or "\\" in value
        or value.startswith(".")
        or value != value.strip()
    ):
        msg = "name must be a single file name, without slashes"
        raise _BadName(msg)
    return value


def _folder(course: str) -> str:
    return f"{proposals.COURSE_ROOT}/{course}"


def _storage_error(e: StorageError) -> JSONResponse:
    if isinstance(e, NotFoundError):
        return error_response(404, "not_found", "No such course folder or file")
    if isinstance(e, PermissionDeniedError):
        return error_response(403, "permission_denied", "Storage refused access; reconnect it")
    logger.warning("Storage read failed (%s)", e.code)
    if isinstance(e, ProviderUnavailableError):
        return error_response(503, "provider_unavailable", "Storage is unreachable; try later")
    return error_response(502, "provider_error", "The storage provider failed")


@router.get("", responses=ERRORS, response_model=list[CourseSummary])
def list_courses(
    user: CurrentUser,
    database: RequiredDatabase,
    storage: DocumentStore,
) -> list[CourseSummary] | JSONResponse:
    """List this user's courses: course folders, and courses with records we created."""
    try:
        folders = {
            info.name for info in storage.list_files(proposals.COURSE_ROOT) if info.type == "folder"
        }
    except NotFoundError:
        folders = set()
    except StorageError as e:
        return _storage_error(e)
    counts: dict[str, dict[str, int]] = {}
    with database.session() as session:
        rows = session.execute(
            select(ExternalRecord.course_key, ExternalRecord.kind, func.count())
            .where(ExternalRecord.user_id == user, ExternalRecord.deleted_at.is_(None))
            .group_by(ExternalRecord.course_key, ExternalRecord.kind),
        )
        for course, kind, count in rows:
            counts.setdefault(course, {})[kind] = count
    return [
        CourseSummary(
            course=course,
            has_folder=course in folders,
            events=counts.get(course, {}).get("event", 0),
            issues=counts.get(course, {}).get("issue", 0),
            documents=counts.get(course, {}).get("document", 0),
        )
        for course in sorted(folders | counts.keys())
    ]


@router.get("/{course}/documents", responses=ERRORS, response_model=list[DocumentEntry])
def list_course_documents(
    course: str,
    _user: CurrentUser,
    storage: DocumentStore,
) -> list[DocumentEntry] | JSONResponse:
    """List the files in this course's folder."""
    try:
        folder = _folder(_course(course))
        entries = storage.list_files(folder)
    except _BadName as e:
        return error_response(400, "invalid_request", str(e))
    except StorageError as e:
        return _storage_error(e)
    return [
        DocumentEntry(
            name=info.name,
            size=info.size,
            modified_at=info.modified_at,
            readable=info.name.lower().endswith(READABLE_SUFFIXES),
        )
        for info in entries
        if info.type != "folder"
    ]


@router.get("/{course}/documents/{name:path}", responses=ERRORS, response_model=DocumentText)
def read_course_document(
    course: str,
    name: str,
    _user: CurrentUser,
    storage: DocumentStore,
) -> DocumentText | JSONResponse:
    """Return the text of one file in this course's folder, capped at 20,000 characters."""
    try:
        path = f"{_folder(_course(course))}/{_document_name(name)}"
    except _BadName as e:
        return error_response(400, "invalid_request", str(e))
    if not name.lower().endswith(READABLE_SUFFIXES):
        return error_response(
            422,
            "invalid_operation",
            "Can't read this file: only PDF, Markdown and plain-text files are supported",
        )
    try:
        size = storage.get_metadata(path).size or 0
        if size > MAX_DOCUMENT_BYTES:
            return error_response(413, "file_too_large", "The file is larger than 10 MB")
        content = storage.download(path)
    except StorageError as e:
        return _storage_error(e)
    text = _pdf_text(content) if name.lower().endswith(".pdf") else _plain_text(content)
    if text is None:
        return error_response(
            422,
            "invalid_operation",
            "Can't read this file: it has no text layer (a scanned PDF?) or is damaged",
        )
    return DocumentText(
        course=course,
        name=name,
        text=text[:MAX_TEXT_CHARACTERS],
        characters=len(text),
        truncated=len(text) > MAX_TEXT_CHARACTERS,
    )


def _plain_text(content: bytes) -> str | None:
    text = content.decode("utf-8", errors="replace")
    return text if text.strip() else None


def _pdf_text(content: bytes) -> str | None:
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, ValueError, KeyError, TypeError, OSError):
        # pypdf raises several kinds of errors on damaged files; all mean "can't read".
        return None
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    return text or None


@router.get("/{course}/plan", responses=ERRORS, response_model=CoursePlan)
def get_course_plan(
    course: str,
    user: CurrentUser,
    database: RequiredDatabase,
    issues: IssueTracker,
    calendar: Calendar,
) -> CoursePlan | JSONResponse:
    """Return the live records our proposals created for this course, read from each provider.

    A record its provider can't return right now is listed with ``available: false``, so one
    provider outage doesn't hide the rest of the plan.
    """
    try:
        _course(course)
    except _BadName as e:
        return error_response(400, "invalid_request", str(e))
    with database.session() as session:
        records = list(
            session.scalars(
                select(ExternalRecord)
                .where(
                    ExternalRecord.user_id == user,
                    ExternalRecord.course_key == course,
                    ExternalRecord.deleted_at.is_(None),
                )
                .order_by(ExternalRecord.created_at),
            ),
        )
    items = []
    for record in records:
        rid = record.provider_record_id
        try:
            if record.kind == "event":
                event = calendar.get(rid)
                items.append(
                    PlanItem(
                        kind="event",
                        id=rid,
                        title=event.title,
                        start=event.start,
                        end=event.end,
                    ),
                )
            elif record.kind == "issue":
                items.append(PlanItem(kind="issue", id=rid, title=issues.get_issue(rid).title))
            else:
                items.append(PlanItem(kind="document", id=rid, title=rid.rsplit("/", 1)[-1]))
        except (CalendarError, ServiceError) as e:
            logger.warning("Plan read of a %s failed (%s)", record.kind, e.code)
            items.append(PlanItem(kind=record.kind, id=rid, title="", available=False))
    return CoursePlan(course=course, items=items)
