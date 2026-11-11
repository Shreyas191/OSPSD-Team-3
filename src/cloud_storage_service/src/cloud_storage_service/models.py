"""Request and response bodies of the HTTP API."""

from datetime import UTC, datetime
from typing import Annotated, Literal

import cloud_storage_client_api
from pydantic import AfterValidator, BaseModel, Field


def clean_path(raw: str) -> str:
    """Return ``raw`` with a leading ``/``, or raise ``ValueError`` if it is not an entry path.

    The vertical's path rule: the leading ``/`` is optional, and a path must not be empty
    or the root, end with ``/``, or contain an empty, ``.`` or ``..`` segment. A rejected
    path becomes ``400 invalid_request`` before the provider is called.
    """
    path = raw if raw.startswith("/") else f"/{raw}"
    if path == "/" or path.endswith("/"):
        msg = "path must not be empty, the root, or end with /"
        raise ValueError(msg)
    if any(part in {"", ".", ".."} for part in path[1:].split("/")):
        msg = "path must not contain empty, '.' or '..' segments"
        raise ValueError(msg)
    return path


def clean_folder_path(raw: str) -> str:
    """Like :func:`clean_path`, but ``""`` and ``/`` mean the root folder and become ``/``."""
    return "/" if raw in {"", "/"} else clean_path(raw)


# A file or folder below the root, e.g. "/Docs/report.pdf" or "Docs/report.pdf".
EntryPath = Annotated[str, AfterValidator(clean_path), Field(examples=["/Docs/report.pdf"])]
# A folder, including the root ("" or "/").
FolderPath = Annotated[str, AfterValidator(clean_folder_path), Field(examples=["/Docs"])]


def utc_timestamp(moment: datetime | None) -> str | None:
    """Return ``moment`` as ISO 8601 in UTC ending in ``Z`` (``2026-10-23T12:00:00Z``)."""
    if moment is None:
        return None
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


class FileResponse(BaseModel):
    """A file or folder as returned by the service (the vertical's file-info object)."""

    id: str
    name: str
    path: str
    size: int | None = Field(description="Size in bytes; null for folders")
    modified_at: str | None = Field(
        description="Last modified time, ISO 8601 in UTC ending in Z; null for folders",
        examples=["2026-10-23T12:00:00Z"],
    )
    etag: str | None = Field(
        description="Fingerprint of the file's current version; it changes whenever the file "
        "changes. Null for folders",
        examples=["015f1a2b3c4d"],
    )

    @classmethod
    def from_file(cls, file: cloud_storage_client_api.File) -> "FileResponse":
        """Build a response from any ``File`` implementation."""
        return cls(
            id=file.id,
            name=file.name,
            path=file.path,
            size=file.size,
            modified_at=utc_timestamp(file.modified),
            etag=file.etag,
        )


class ListEntry(FileResponse):
    """One entry of a folder listing: the file info plus whether it is a file or folder."""

    type: Literal["file", "folder"]

    @classmethod
    def from_file(cls, file: cloud_storage_client_api.File) -> "ListEntry":
        """Build a listing entry from any ``File`` implementation."""
        info = FileResponse.from_file(file).model_dump()
        return cls(**info, type="folder" if file.is_folder else "file")


class CreateFolderRequest(BaseModel):
    """Create a new folder at the specified path."""

    remote_path: EntryPath = Field(
        description="Destination path in cloud storage",
        examples=["/Docs/NewFolder"],
    )


class CopyRequest(BaseModel):
    """Copy the entry at ``from_path`` to the full destination path ``to_path``."""

    from_path: EntryPath
    to_path: EntryPath = Field(examples=["/Archive/report.pdf"])


class RenameRequest(BaseModel):
    """Rename the entry at ``path`` to ``new_name`` within the same folder."""

    path: EntryPath
    new_name: str = Field(min_length=1, pattern=r"^[^/]+$", examples=["summary.pdf"])


class MoveRequest(BaseModel):
    """Move the entry at ``from_path`` to the full destination path ``to_path``."""

    from_path: EntryPath
    to_path: EntryPath = Field(examples=["/Archive/report.pdf"])


class PatchRequest(BaseModel):
    """Move or rename the entry at the ``path`` query parameter to ``new_path``."""

    new_path: EntryPath = Field(examples=["/Archive/report.pdf"])
