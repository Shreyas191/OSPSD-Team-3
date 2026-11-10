"""Real Dropbox SDK objects for unit tests of ``dropbox_client_impl``.

Tests build the SDK's own metadata and error types instead of ``Mock()`` stand-ins, so the
code under test reads them exactly as it would read a Dropbox response.
"""

# ``tests`` is deliberately not a package (see tests/fakes.py).
# ruff: noqa: INP001

from datetime import UTC, datetime

from dropbox import exceptions, files

MODIFIED = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)


def file_metadata(
    path: str = "/Docs/report.pdf",
    *,
    file_id: str = "id:file123",
    size: int = 2048,
    rev: str = "015f1a2b3c4d",
) -> files.FileMetadata:
    """Return the metadata Dropbox sends for a file at ``path``."""
    return files.FileMetadata(
        name=path.rsplit("/", 1)[-1],
        id=file_id,
        path_lower=path.lower(),
        path_display=path,
        client_modified=MODIFIED,
        server_modified=MODIFIED,
        rev=rev,
        size=size,
    )


def folder_metadata(path: str = "/Photos", *, folder_id: str = "id:folder456") -> files.FolderMetadata:
    """Return the metadata Dropbox sends for a folder at ``path``."""
    return files.FolderMetadata(
        name=path.rsplit("/", 1)[-1],
        id=folder_id,
        path_lower=path.lower(),
        path_display=path,
    )


def api_error(reason: object) -> exceptions.ApiError:
    """Return the exception the SDK raises when Dropbox answers with the error ``reason``."""
    return exceptions.ApiError("request-id", reason, None, None)


def server_error() -> exceptions.InternalServerError:
    """Return the exception the SDK raises for a Dropbox 5xx response."""
    return exceptions.InternalServerError("request-id", 500, "internal error")


def rate_limited() -> exceptions.RateLimitError:
    """Return the exception the SDK raises when Dropbox rate limits the request."""
    return exceptions.RateLimitError("request-id")
