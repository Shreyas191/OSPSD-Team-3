"""The daily database backup (backup.py): pg_dump and Cloud Storage both faked.

No test connects to a database or to Google: ``pg_dump`` is a stand-in for
``subprocess.run`` and Cloud Storage is an ``httpx.MockTransport``. What's checked is what
the job asks each of them for, and that no failure message carries the connection string.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from cloud_storage_service import backup
from cloud_storage_service.backup import (
    BUCKET_ENV_VAR,
    DATABASE_URL_ENV_VAR,
    PREFIX_ENV_VAR,
    BackupError,
    Settings,
    direct_url,
    dump,
    object_name,
    run_backup,
    upload,
)

PASSWORD = "npg_s3cretPassw0rd"
POOLED = f"postgresql://course:{PASSWORD}@ep-quiet-sun-a1b2c3-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
SETTINGS = Settings(POOLED, "ospsd-team3-database-backups", "demo")
NOW = datetime(2026, 12, 2, 9, 0, 5, tzinfo=UTC)


class FakePgDump:
    """Stands in for ``subprocess.run``: writes a dump file, or fails with ``returncode``."""

    def __init__(self, returncode: int = 0, raises: Exception | None = None) -> None:
        """Choose how ``pg_dump`` behaves."""
        self.returncode = returncode
        self.raises = raises
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Record the command; write the file as pg_dump would."""
        assert kwargs["capture_output"] is True  # its error text never reaches our output
        self.commands.append(command)
        if self.raises is not None:
            raise self.raises
        if self.returncode == 0:
            path = next(a.removeprefix("--file=") for a in command if a.startswith("--file="))
            Path(path).write_bytes(b"PGDMP fake dump")
        stderr = f"connection to {POOLED} failed".encode()
        return subprocess.CompletedProcess(command, self.returncode, b"", stderr)


class FakeStorage:
    """Cloud Storage's upload endpoint: keeps what it receives."""

    def __init__(self, status: int = 200) -> None:
        """Answer every upload with ``status``."""
        self.status = status
        self.uploads: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Record the upload."""
        self.uploads.append(request)
        return httpx.Response(self.status, json={})

    def client(self) -> httpx.Client:
        """Return a client whose requests this fake answers."""
        return httpx.Client(transport=httpx.MockTransport(self.handle))


# ----- Connection string -----


def test_the_pooled_neon_endpoint_becomes_the_direct_one() -> None:
    """pg_dump can't go through PgBouncer: ``-pooler`` is dropped, and nothing else."""
    assert direct_url(POOLED) == (
        f"postgresql://course:{PASSWORD}@ep-quiet-sun-a1b2c3.us-east-2.aws.neon.tech/neondb"
        "?sslmode=require"
    )


def test_a_driver_suffix_is_dropped_and_a_direct_url_kept() -> None:
    """SQLAlchemy's ``postgresql+psycopg`` becomes ``postgresql``; a direct host is unchanged."""
    url = "postgresql+psycopg://u:p@db.example.test:5432/app"

    assert direct_url(url) == "postgresql://u:p@db.example.test:5432/app"


# ----- pg_dump -----


def test_pg_dump_writes_a_custom_format_dump_of_the_direct_endpoint(tmp_path: Path) -> None:
    """The command asks for a custom-format dump, no owners or privileges, to the given file."""
    pg_dump, path = FakePgDump(), tmp_path / "db.dump"

    dump(POOLED, path, pg_dump)

    [command] = pg_dump.commands
    assert command[:4] == ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    assert f"--file={path}" in command
    assert f"--dbname={direct_url(POOLED)}" in command
    assert path.read_bytes().startswith(b"PGDMP")


@pytest.mark.parametrize(
    ("pg_dump", "reason"),
    [
        (FakePgDump(returncode=1), "exit status 1"),
        (FakePgDump(raises=FileNotFoundError()), "isn't installed"),
        (FakePgDump(raises=subprocess.TimeoutExpired("pg_dump", 600)), "longer than"),
    ],
)
def test_a_failed_dump_says_why_without_the_connection_string(
    tmp_path: Path,
    pg_dump: FakePgDump,
    reason: str,
) -> None:
    """Only the exit status or the cause is reported; pg_dump's own text, naming the host, isn't."""
    with pytest.raises(BackupError, match=reason) as raised:
        dump(POOLED, tmp_path / "db.dump", pg_dump)

    assert PASSWORD not in str(raised.value)
    assert "neon.tech" not in str(raised.value)


# ----- Upload -----


def test_the_upload_goes_to_the_bucket_and_never_replaces_a_backup(tmp_path: Path) -> None:
    """One media upload, with ``ifGenerationMatch=0`` so an existing object is never replaced."""
    storage, path = FakeStorage(), tmp_path / "db.dump"
    path.write_bytes(b"PGDMP data")

    upload(storage.client(), "token-123", "my-bucket", "demo/2026-12-02T090005Z.dump", path)

    [request] = storage.uploads
    assert request.url.path == "/upload/storage/v1/b/my-bucket/o"
    assert dict(request.url.params) == {
        "uploadType": "media",
        "name": "demo/2026-12-02T090005Z.dump",
        "ifGenerationMatch": "0",
    }
    assert request.headers["Authorization"] == "Bearer token-123"
    assert request.content == b"PGDMP data"


@pytest.mark.parametrize("status", [403, 412, 503])
def test_a_refused_upload_fails_the_backup(tmp_path: Path, status: int) -> None:
    """Refused (no permission, already exists, unavailable): the backup fails, with the status."""
    path = tmp_path / "db.dump"
    path.write_bytes(b"x")

    with pytest.raises(BackupError, match=f"HTTP {status}"):
        upload(FakeStorage(status).client(), "t", "b", "n", path)


def test_cloud_storage_unreachable_fails_the_backup(tmp_path: Path) -> None:
    """A network failure is a failed backup."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        msg = "no route"
        raise httpx.ConnectError(msg, request=request)

    path = tmp_path / "db.dump"
    path.write_bytes(b"x")

    with pytest.raises(BackupError, match="can't be reached"):
        upload(httpx.Client(transport=httpx.MockTransport(unreachable)), "t", "b", "n", path)


# ----- The whole job -----


def test_a_backup_dumps_uploads_and_reports_what_it_wrote() -> None:
    """The job's summary names the object, its size and duration; never the database."""
    storage = FakeStorage()
    ticks = iter([10.0, 12.5])

    line = run_backup(
        SETTINGS,
        storage.client(),
        token=lambda: "t",
        run=FakePgDump(),
        now=lambda: NOW,
        clock=lambda: next(ticks),
    )

    assert line == {
        "event": "database_backup",
        "outcome": "ok",
        "object": "gs://ospsd-team3-database-backups/demo/2026-12-02T090005Z.dump",
        "bytes": len(b"PGDMP fake dump"),
        "duration_ms": 2500,
    }
    assert storage.uploads[0].content == b"PGDMP fake dump"
    assert object_name("demo", NOW) == "demo/2026-12-02T090005Z.dump"


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three are needed; a missing one is named, with no values in the message."""
    for name in (DATABASE_URL_ENV_VAR, BUCKET_ENV_VAR, PREFIX_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(DATABASE_URL_ENV_VAR, POOLED)

    with pytest.raises(BackupError) as raised:
        Settings.from_env()

    assert str(raised.value) == f"Missing {BUCKET_ENV_VAR}, {PREFIX_ENV_VAR}"
    monkeypatch.setenv(BUCKET_ENV_VAR, "b")
    monkeypatch.setenv(PREFIX_ENV_VAR, "demo")
    assert Settings.from_env() == Settings(POOLED, "b", "demo")


@pytest.mark.parametrize("fails", [False, True])
def test_main_prints_one_line_and_exits_with_the_outcome(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fails: bool,  # noqa: FBT001
) -> None:
    """0 and an ``ok`` line on success; 1 and a ``failed`` line, without secrets, on failure."""
    monkeypatch.setenv(DATABASE_URL_ENV_VAR, POOLED)
    monkeypatch.setenv(BUCKET_ENV_VAR, "b")
    monkeypatch.setenv(PREFIX_ENV_VAR, "demo")

    def fake_run(_settings: Settings, _http: httpx.Client) -> dict[str, Any]:
        if fails:
            msg = "pg_dump failed with exit status 1"
            raise BackupError(msg)
        return {"event": "database_backup", "outcome": "ok"}

    monkeypatch.setattr(backup, "run_backup", fake_run)

    status = backup.main()

    line = json.loads(capsys.readouterr().out)
    assert status == (1 if fails else 0)
    assert line["outcome"] == ("failed" if fails else "ok")
    assert PASSWORD not in json.dumps(line)


def test_the_token_comes_from_the_jobs_own_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Application default credentials, scoped to Cloud Storage; no token is a failure."""

    class Credentials:
        token: str | None = "ya29.token"

        def refresh(self, _request: object) -> None:
            pass

    seen: list[list[str]] = []
    credentials = Credentials()

    def default(*, scopes: list[str]) -> tuple[Credentials, str]:
        seen.append(scopes)
        return credentials, "ospsd-team3"

    monkeypatch.setattr("google.auth.default", default)

    assert backup.storage_token() == "ya29.token"
    assert seen == [[backup.STORAGE_SCOPE]]
    credentials.token = None
    with pytest.raises(BackupError, match="No access token"):
        backup.storage_token()


def test_an_old_pg_dump_is_named_with_both_versions(tmp_path: Path) -> None:
    """Neon runs PostgreSQL 18: an older pg_dump refuses, and the error says so plainly."""

    def old_pg_dump(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        stderr = (
            b"pg_dump: error: aborting because of server version mismatch\n"
            b"pg_dump: detail: server version: 18.7 (3190d2f); pg_dump version: 17.10\n"
        )
        return subprocess.CompletedProcess(command, 1, b"", stderr)

    with pytest.raises(BackupError) as raised:
        dump(POOLED, tmp_path / "db.dump", old_pg_dump)

    assert str(raised.value) == (
        "pg_dump 17.10 is older than the database server 18.7; install a newer "
        "postgresql-client in the image"
    )
