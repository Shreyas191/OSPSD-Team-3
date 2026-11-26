"""Who is calling a product route (HW3 Milestone 2).

Sign-in (Google, sessions) is issue #96 and #97; until it lands, product routes accept the
``X-Dev-User`` header, and only when ``DEV_SIGN_IN=enabled`` on a service other than demo.
Otherwise every product route answers 401. The header names a development user, created on
first use with ``auth_provider="dev"``, so tests and the test deployment can exercise
proposals before real accounts exist. #97 replaces this module's lookup with the session.

A user ID in a request body or a tool argument is never used: the acting user comes only
from here.
"""

import os
import re
import uuid
from typing import Annotated

from course_autopilot_store import Database
from course_autopilot_store.models import User
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select

from cloud_storage_service.database import DatabaseDependency
from cloud_storage_service.faults import DEMO_SERVICE

DEV_SIGN_IN_ENV_VAR = "DEV_SIGN_IN"
DEV_AUTH_PROVIDER = "dev"
_DEV_SUBJECT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def dev_sign_in_enabled() -> bool:
    """Whether ``X-Dev-User`` is accepted: opted in, and never on the demo service."""
    return (
        os.environ.get(DEV_SIGN_IN_ENV_VAR) == "enabled"
        and os.environ.get("K_SERVICE") != DEMO_SERVICE
    )


def require_database(database: DatabaseDependency) -> Database:
    """Return the database, or answer 503 when the service has none."""
    if database is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No database is configured")
    return database


RequiredDatabase = Annotated[Database, Depends(require_database)]


def get_current_user(
    database: RequiredDatabase,
    x_dev_user: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """Return the acting user's ID, or answer 401."""
    if not dev_sign_in_enabled() or x_dev_user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
    if not _DEV_SUBJECT.fullmatch(x_dev_user):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
    with database.session() as session:
        user = session.scalars(
            select(User).where(
                User.auth_provider == DEV_AUTH_PROVIDER,
                User.auth_subject == x_dev_user,
            ),
        ).first()
        if user is None:
            user = User(auth_provider=DEV_AUTH_PROVIDER, auth_subject=x_dev_user)
            session.add(user)
            session.flush()
        return user.id


CurrentUser = Annotated[uuid.UUID, Depends(get_current_user)]
