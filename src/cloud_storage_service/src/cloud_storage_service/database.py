"""The persisted database (HW3): built from ``DATABASE_URL``, migrated at startup, health-checked.

``DATABASE_URL`` comes from Secret Manager in the deployed services (infra/run.tf). Without
it, for example in local development and in tests, the service runs without a database and
``GET /health/database`` says so.

A failed migration doesn't stop the service: the storage and workflow routes don't need the
database yet, and a database outage must not take them down. The failure is logged and
``GET /health/database`` answers 503, which fails the deploy's smoke check, so a release that
can't reach its database never receives traffic.
"""

import logging
import os
from typing import Annotated

from course_autopilot_store import Database
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from cloud_storage_service.errors import ErrorResponse, error_response

DATABASE_URL_ENV_VAR = "DATABASE_URL"

logger = logging.getLogger(__name__)


def build_database() -> Database | None:
    """Build the database from ``DATABASE_URL``, or return ``None`` when it isn't set."""
    url = os.environ.get(DATABASE_URL_ENV_VAR, "")
    return Database.from_url(url) if url else None


def migrate(database: Database) -> bool:
    """Apply pending migrations. Return whether that worked; log, never raise, on failure."""
    try:
        database.upgrade()
    except SQLAlchemyError as e:
        # The class name only: a driver message can contain the host or user name.
        logger.warning("Database migration failed at startup (%s)", type(e).__name__)
        return False
    return True


def get_database(request: Request) -> Database | None:
    """Return the database built at startup, or ``None`` when none is configured."""
    database: Database | None = getattr(request.app.state, "database", None)
    return database


DatabaseDependency = Annotated[Database | None, Depends(get_database)]

router = APIRouter()


@router.get(
    "/health/database",
    status_code=status.HTTP_200_OK,
    responses={503: {"model": ErrorResponse, "description": "No database, or it is unreachable"}},
    response_model=None,
)
def check_database_health(database: DatabaseDependency) -> dict[str, str] | JSONResponse:
    """Check that the database is configured and answers one query."""
    if database is None:
        return error_response(503, "database_not_configured", "No database is configured")
    try:
        database.check()
    except SQLAlchemyError as e:
        logger.warning("Database health check failed (%s)", type(e).__name__)
        return error_response(503, "database_unavailable", "The database is unreachable")
    return {"status": "ok"}
