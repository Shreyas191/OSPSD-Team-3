"""A logical backup of this environment's database, to a private bucket (docs/database-backup.md).

Neon's free plan can restore a branch only within the last 6 hours, so damage noticed the
next morning couldn't be undone (#120, section 6). During launch week a Cloud Run job runs
this once a day for ``demo`` (``infra/backup.tf``; entry point ``backup_main.py``):

1. ``pg_dump --format=custom`` of ``DATABASE_URL`` into a temporary file. Neon's pooled
   connection string (host ``ep-…-pooler…``) goes through PgBouncer, which ``pg_dump`` doesn't
   support, so the job connects to the same endpoint without ``-pooler``.
2. Upload to ``gs://<BACKUP_BUCKET>/<BACKUP_PREFIX>/<UTC time>.dump``, never replacing an
   existing object. The bucket deletes dumps after 14 days.

The job's identity can read only its environment's database URL and create objects in the
bucket: it can't read, replace or delete a backup. Nothing here logs the connection string:
``pg_dump``'s own error text can name the host and user, so only its exit status is kept.
The dump holds conversation text, and provider tokens still encrypted; restoring it needs the
same ``token-encryption-key-<env>``.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

DATABASE_URL_ENV_VAR = "DATABASE_URL"
BUCKET_ENV_VAR = "BACKUP_BUCKET"
PREFIX_ENV_VAR = "BACKUP_PREFIX"
UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
STORAGE_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
DUMP_TIMEOUT_SECONDS = 600
UPLOAD_TIMEOUT_SECONDS = 120.0

_VERSIONS = re.compile(
    r"server version: (?P<server>\d+(?:\.\d+)*).*?pg_dump version: (?P<client>\d+(?:\.\d+)*)",
    re.DOTALL,
)

# Runs pg_dump: the same signature as subprocess.run, so tests can stand in for it.
Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class BackupError(Exception):
    """The backup failed. The message never contains the connection string."""


@dataclass(frozen=True)
class Settings:
    """Where to read from and where to write to."""

    database_url: str
    bucket: str
    prefix: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Read the three settings.

        Raises:
            BackupError: If one is missing; the message names it, never a value.

        """
        names = (DATABASE_URL_ENV_VAR, BUCKET_ENV_VAR, PREFIX_ENV_VAR)
        values = [os.environ.get(name, "").strip() for name in names]
        missing = [name for name, value in zip(names, values, strict=True) if not value]
        if missing:
            msg = f"Missing {', '.join(missing)}"
            raise BackupError(msg)
        return cls(*values)


def direct_url(url: str) -> str:
    """Return a ``pg_dump``-ready URL: plain ``postgresql://``, and Neon's endpoint unpooled."""
    parts = urlsplit(url)
    scheme = "postgresql" if parts.scheme.startswith("postgres") else parts.scheme
    netloc = parts.netloc
    host = parts.hostname or ""
    if "-pooler." in host:
        netloc = netloc.replace(host, host.replace("-pooler.", ".", 1), 1)
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def object_name(prefix: str, now: datetime) -> str:
    """Return the backup's object name, e.g. ``demo/2026-12-02T090000Z.dump``."""
    return f"{prefix}/{now.astimezone(UTC):%Y-%m-%dT%H%M%SZ}.dump"


def dump(url: str, path: Path, run: Runner = subprocess.run) -> None:
    """Write a custom-format dump of the database at ``url`` to ``path``.

    Raises:
        BackupError: If ``pg_dump`` fails or is missing; only its exit status is reported.

    """
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        f"--file={path}",
        f"--dbname={direct_url(url)}",
    ]
    try:
        result = run(command, capture_output=True, check=False, timeout=DUMP_TIMEOUT_SECONDS)
    except FileNotFoundError:
        msg = "pg_dump isn't installed in this image"
        raise BackupError(msg) from None
    except subprocess.TimeoutExpired:
        msg = f"pg_dump took longer than {DUMP_TIMEOUT_SECONDS} s"
        raise BackupError(msg) from None
    if result.returncode != 0:
        # pg_dump's text can name the host and user; only the version numbers are kept.
        versions = _VERSIONS.search(result.stderr.decode(errors="replace"))
        if versions:
            msg = (
                f"pg_dump {versions['client']} is older than the database server "
                f"{versions['server']}; install a newer postgresql-client in the image"
            )
        else:
            msg = f"pg_dump failed with exit status {result.returncode}"
        raise BackupError(msg)


def upload(http: httpx.Client, token: str, bucket: str, name: str, path: Path) -> None:
    """Upload ``path`` as ``name``; refuse to replace an object that already exists.

    Raises:
        BackupError: If Cloud Storage refuses it or can't be reached.

    """
    try:
        response = http.post(
            UPLOAD_URL.format(bucket=quote(bucket, safe="")),
            params={"uploadType": "media", "name": name, "ifGenerationMatch": "0"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            content=path.read_bytes(),
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        msg = f"Cloud Storage can't be reached ({type(e).__name__})"
        raise BackupError(msg) from None
    if not response.is_success:
        msg = f"Cloud Storage refused the upload (HTTP {response.status_code})"
        raise BackupError(msg)


def storage_token() -> str:
    """Return an access token for Cloud Storage from the job's own identity."""
    import google.auth  # noqa: PLC0415
    from google.auth.transport.requests import Request  # noqa: PLC0415

    credentials, _ = google.auth.default(scopes=[STORAGE_SCOPE])  # type: ignore[no-untyped-call]
    credentials.refresh(Request())  # type: ignore[no-untyped-call]
    token = credentials.token
    if not token:
        msg = "No access token for Cloud Storage from the job's identity"
        raise BackupError(msg)
    return str(token)


def run_backup(  # noqa: PLR0913 (the backup's inputs; each replaceable in tests)
    settings: Settings,
    http: httpx.Client,
    *,
    token: Callable[[], str] = storage_token,
    run: Runner = subprocess.run,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Dump the database and upload it; return what to log about the backup.

    Raises:
        BackupError: If any step fails.

    """
    started = clock()
    name = object_name(settings.prefix, now())
    with tempfile.TemporaryDirectory(prefix="backup-") as directory:
        path = Path(directory) / "database.dump"
        dump(settings.database_url, path, run)
        size = path.stat().st_size
        upload(http, token(), settings.bucket, name, path)
    return {
        "event": "database_backup",
        "outcome": "ok",
        "object": f"gs://{settings.bucket}/{name}",
        "bytes": size,
        "duration_ms": round((clock() - started) * 1000),
    }


def main() -> int:
    """Run one backup; print one JSON line; return the process exit status."""
    try:
        settings = Settings.from_env()
        with httpx.Client() as http:
            line = run_backup(settings, http)
    except BackupError as e:
        print(json.dumps({"event": "database_backup", "outcome": "failed", "reason": str(e)}))  # noqa: T201
        return 1
    print(json.dumps(line))  # noqa: T201
    return 0
