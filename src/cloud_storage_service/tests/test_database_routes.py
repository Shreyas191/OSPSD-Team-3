"""The API service's database: ``GET /health/database``, and migration at startup.

Refer to ``database.py``: without ``DATABASE_URL`` the service runs without a database; a
failed migration is logged and reported by the health check instead of stopping the service.
"""

import importlib
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from course_autopilot_store import Database
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app
from cloud_storage_service.database import (
    DATABASE_URL_ENV_VAR,
    build_database,
    get_database,
    migrate,
)

UNREACHABLE_URL = "postgresql://user:not-a-real-password@127.0.0.1:1/neondb"


@pytest.fixture
def with_database() -> Iterator[Mock]:
    """Install a mock database for the duration of a test."""
    database = Mock(spec=Database)
    app.dependency_overrides[get_database] = lambda: database
    yield database
    app.dependency_overrides.clear()


def test_health_is_ok_when_the_database_answers(with_database: Mock) -> None:
    """One successful query means 200."""
    response = TestClient(app).get("/health/database")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    with_database.check.assert_called_once_with()


def test_health_is_503_when_the_database_is_unreachable(with_database: Mock) -> None:
    """A failed query means 503 in the shared error body, with no connection details."""
    with_database.check.side_effect = OperationalError("SELECT 1", {}, Exception("host db.x"))

    response = TestClient(app).get("/health/database")

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "database_unavailable", "message": "The database is unreachable"},
    }


def test_health_is_503_when_no_database_is_configured() -> None:
    """The service runs without a database, but the health check says so."""
    app.dependency_overrides[get_database] = lambda: None
    try:
        response = TestClient(app).get("/health/database")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_not_configured"


def test_no_database_url_means_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local development and the existing tests need no database."""
    monkeypatch.delenv(DATABASE_URL_ENV_VAR, raising=False)

    assert build_database() is None


def test_a_database_url_builds_the_database_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building makes no connection, so an unreachable database can't block startup here."""
    monkeypatch.setenv(DATABASE_URL_ENV_VAR, UNREACHABLE_URL)

    database = build_database()

    assert database is not None
    assert database.engine.dialect.name == "postgresql"


def test_a_failed_migration_is_logged_without_details(caplog: pytest.LogCaptureFixture) -> None:
    """``migrate`` reports failure by its return value and logs only the error's class."""
    database = Mock(spec=Database)
    database.upgrade.side_effect = OperationalError("x", {}, Exception("password=hunter2"))

    with caplog.at_level(logging.WARNING):
        assert migrate(database) is False

    assert "OperationalError" in caplog.text
    assert "hunter2" not in caplog.text


def _start(monkeypatch: pytest.MonkeyPatch, database_url: str) -> TestClient:
    monkeypatch.setattr(
        importlib.import_module("cloud_storage_service.app"),
        "build_storage_client",
        FakeStorageClient,
    )
    for name in ("ISSUE_TRACKER_IMPL", "CALENDAR_IMPL", "K_SERVICE", "FAULT_INJECTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(DATABASE_URL_ENV_VAR, database_url)
    return TestClient(app)


def test_startup_migrates_the_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With a reachable database, startup creates the tables before serving."""
    url = f"sqlite:///{tmp_path / 'app.db'}"

    with _start(monkeypatch, url) as http:
        assert http.get("/health/database").status_code == 200

    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"users", "proposals", "approvals", "alembic_version"} <= tables


def test_an_unreachable_database_does_not_stop_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """The storage routes keep working; only the database health check fails."""
    with _start(monkeypatch, UNREACHABLE_URL) as http:
        assert http.get("/health").status_code == 200
        assert http.get("/health/database").status_code == 503
