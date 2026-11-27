"""The model budget: how many Gemini calls a user and the whole project may make (#102, T12).

Gemini's free tier allows 15 calls a minute and 500 a day for the project (DESIGN.md Q4, the
readiness record). The agent service asks here before **every** model call, so the limits
hold however many agent instances run. The counters live in ``usage_counters``, so the
agent service itself never touches the database (T14).

- ``POST /usage/model-calls`` reserves one call for the caller (an agent token is enough):
  - ``200`` with what's left today;
  - ``429 daily_limit_reached``: the user has made ``MODEL_CALLS_PER_USER_PER_DAY`` calls
    (default 25, about 3 course setups) today, or the whole project has made
    ``MODEL_CALLS_PER_PROJECT_PER_DAY`` (default 500, Gemini's daily allowance). Days start at
    midnight Pacific time, when Gemini's own daily allowance resets. Refusing here is cleaner
    than Gemini failing partway through a turn;
  - ``429 model_rate_limited`` with ``retry_after_seconds``: the project has made
    ``MODEL_CALLS_PER_MINUTE`` calls (default 15) this minute. The agent waits and asks again,
    so a turn queues instead of failing partway through.
- ``GET /usage/model-calls``: today's use and limit, for the interface.

A refused reservation counts nothing. All three counters change in one transaction, each with
an atomic conditional increment, so two instances can't both take the last call. The
project's daily count is the ``project-day`` row of ``usage_counters``, so an alert can watch
how much of Gemini's allowance is used.
"""

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from course_autopilot_store import Database
from course_autopilot_store.models import UsageCounter
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from cloud_storage_service.configuration import ConfigurationError
from cloud_storage_service.errors import ErrorResponse, SharedErrorRoute, error_response
from cloud_storage_service.identity import CurrentUser, RequiredDatabase

PER_USER_PER_DAY_ENV_VAR = "MODEL_CALLS_PER_USER_PER_DAY"
PER_MINUTE_ENV_VAR = "MODEL_CALLS_PER_MINUTE"
PER_PROJECT_PER_DAY_ENV_VAR = "MODEL_CALLS_PER_PROJECT_PER_DAY"
DEFAULT_PER_USER_PER_DAY = 25
DEFAULT_PER_MINUTE = 15
DEFAULT_PER_PROJECT_PER_DAY = 500
# Gemini's daily allowance resets at midnight Pacific time; ours does too.
DAY_ZONE = ZoneInfo("America/Los_Angeles")
PROJECT_KEY = "project"
PROJECT_DAY_KEY = "project-day"
# Counters older than this are no longer read; each reservation removes them.
KEEP = timedelta(days=2)

router = APIRouter(prefix="/usage", tags=["usage"], route_class=SharedErrorRoute)


@dataclass(frozen=True)
class Limits:
    """Model calls allowed per user per day, per project per minute and per project per day."""

    per_user_per_day: int = DEFAULT_PER_USER_PER_DAY
    per_minute: int = DEFAULT_PER_MINUTE
    per_project_per_day: int = DEFAULT_PER_PROJECT_PER_DAY

    @classmethod
    def from_env(cls) -> "Limits":
        """Read the limits; a value that isn't a positive whole number stops the service."""
        values = []
        for name, default in (
            (PER_USER_PER_DAY_ENV_VAR, DEFAULT_PER_USER_PER_DAY),
            (PER_MINUTE_ENV_VAR, DEFAULT_PER_MINUTE),
            (PER_PROJECT_PER_DAY_ENV_VAR, DEFAULT_PER_PROJECT_PER_DAY),
        ):
            raw = os.environ.get(name, "").strip()
            try:
                value = int(raw) if raw else default
            except ValueError:
                value = 0
            if value < 1:
                msg = f"{name} must be a whole number of at least 1"
                raise ConfigurationError(msg)
            values.append(value)
        return cls(*values)


class DailyLimitError(Exception):
    """The user, or the whole project, has used today's model calls."""

    def __init__(self, resets_at: datetime, *, project: bool = False) -> None:
        """Record when the next day starts, and whether it's the project's limit."""
        super().__init__("daily limit reached")
        self.resets_at = resets_at
        self.project = project


class RateLimitedError(Exception):
    """The project has used this minute's model calls."""

    def __init__(self, retry_after: int) -> None:
        """Record how many seconds until the next minute."""
        super().__init__("rate limited")
        self.retry_after = retry_after


def day_start(now: datetime) -> datetime:
    """Return the start, in UTC, of the Pacific-time day that contains ``now``."""
    local = now.astimezone(DAY_ZONE)
    return datetime.combine(local.date(), time(), DAY_ZONE).astimezone(UTC)


def next_day_start(now: datetime) -> datetime:
    """Return when the next Pacific-time day starts, in UTC."""
    local_date = now.astimezone(DAY_ZONE).date() + timedelta(days=1)
    return datetime.combine(local_date, time(), DAY_ZONE).astimezone(UTC)


def _minute_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(second=0, microsecond=0)


def _user_key(user_id: uuid.UUID) -> str:
    return f"user:{user_id}"


def _take(session: Session, key: str, window: datetime, limit: int) -> bool:
    """Add one to the counter if it is under ``limit``; return whether it was."""
    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    statement = (
        insert(UsageCounter)
        .values(key=key, window_start=window, count=1)
        .on_conflict_do_update(
            index_elements=[UsageCounter.key, UsageCounter.window_start],
            set_={"count": UsageCounter.count + 1},
            where=UsageCounter.count < limit,
        )
        .returning(UsageCounter.count)
    )
    return session.execute(statement).first() is not None


def _used(session: Session, key: str, window: datetime) -> int:
    count = session.scalars(
        select(UsageCounter.count).where(
            UsageCounter.key == key,
            UsageCounter.window_start == window,
        ),
    ).first()
    return int(count or 0)


def reserve(database: Database, user_id: uuid.UUID, limits: Limits, now: datetime) -> int:
    """Reserve one model call for this user now; return how many are left today.

    Raises:
        DailyLimitError: The user, or the project, has none left today. Nothing is counted.
        RateLimitedError: The project has none left this minute. Nothing is counted.

    """
    day, minute = day_start(now), _minute_start(now)
    with database.session() as session:
        session.execute(delete(UsageCounter).where(UsageCounter.window_start < now - KEEP))
    with database.session() as session:
        if not _take(session, _user_key(user_id), day, limits.per_user_per_day):
            raise DailyLimitError(next_day_start(now))
        if not _take(session, PROJECT_DAY_KEY, day, limits.per_project_per_day):
            # Leaving the block with an error rolls back the user's count too.
            raise DailyLimitError(next_day_start(now), project=True)
        if not _take(session, PROJECT_KEY, minute, limits.per_minute):
            # Leaving the block with an error rolls back the user's count too.
            raise RateLimitedError(max(1, 60 - now.astimezone(UTC).second))
        return limits.per_user_per_day - _used(session, _user_key(user_id), day)


class Reserved(BaseModel):
    """One model call reserved; how many the user has left today."""

    remaining_today: int


class Usage(BaseModel):
    """The user's model calls today."""

    used_today: int
    limit_per_day: int
    resets_at: datetime


ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"},
    429: {"model": ErrorResponse, "description": "Today's limit reached, or this minute's"},
    503: {"model": ErrorResponse, "description": "No database is configured"},
}


@router.post("/model-calls", responses=ERRORS, response_model=Reserved)
def reserve_model_call(user: CurrentUser, database: RequiredDatabase) -> Reserved | JSONResponse:
    """Reserve one model call for the caller, before the agent makes it."""
    limits = Limits.from_env()
    try:
        remaining = reserve(database, user, limits, datetime.now(UTC))
    except DailyLimitError as e:
        whose = (
            "The assistant has reached today's limit for everyone"
            if e.project
            else ("You've reached today's limit for the assistant")
        )
        return error_response(
            429,
            "daily_limit_reached",
            f"{whose}; your course plans and approvals still work",
            headers={"Retry-After": str(int((e.resets_at - datetime.now(UTC)).total_seconds()))},
        )
    except RateLimitedError as e:
        return error_response(
            429,
            "model_rate_limited",
            "The assistant is busy; your turn continues in a moment",
            headers={"Retry-After": str(e.retry_after)},
        )
    return Reserved(remaining_today=remaining)


@router.get("/model-calls", responses=ERRORS)
def model_call_usage(user: CurrentUser, database: RequiredDatabase) -> Usage:
    """Return the caller's model calls today, and when the count starts again."""
    limits = Limits.from_env()
    now = datetime.now(UTC)
    with database.session() as session:
        used = _used(session, _user_key(user), day_start(now))
    return Usage(
        used_today=used,
        limit_per_day=limits.per_user_per_day,
        resets_at=next_day_start(now),
    )
