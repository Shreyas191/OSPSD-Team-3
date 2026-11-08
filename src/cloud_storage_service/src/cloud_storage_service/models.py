"""Request and response bodies of the HTTP API."""

from datetime import datetime
from typing import Annotated

import cloud_storage_client_api
from pydantic import BaseModel, Field

# One or more "/name" segments: no empty segments ("/a//b") and no trailing slash
# ("/a/"). Dropbox rejects those as malformed paths, so they are refused here with 422
# instead of reaching the provider. The root ("/") is not an entry path either.
ABSOLUTE_PATH_PATTERN = r"^(/[^/]+)+$"
AbsolutePath = Annotated[str, Field(pattern=ABSOLUTE_PATH_PATTERN, examples=["/Docs/report.pdf"])]


class FileResponse(BaseModel):
    """A file or folder as returned by the service."""

    id: str
    name: str
    path: str
    is_folder: bool
    size: int | None = Field(description="Size in bytes; null for folders")
    modified: datetime | None = Field(description="Last modified time; null for folders")

    @classmethod
    def from_file(cls, file: cloud_storage_client_api.File) -> "FileResponse":
        """Build a response from any ``File`` implementation."""
        return cls(
            id=file.id,
            name=file.name,
            path=file.path,
            is_folder=file.is_folder,
            size=file.size,
            modified=file.modified,
        )


class CreateFolderRequest(BaseModel):
    """Create a new folder at the specified path."""

    remote_path: AbsolutePath = Field(
        description="Destination path in cloud storage",
        examples=["/Docs/NewFolder"],
    )


class CopyRequest(BaseModel):
    """Copy the entry at ``from_path`` to the full destination path ``to_path``."""

    from_path: AbsolutePath
    to_path: AbsolutePath = Field(examples=["/Archive/report.pdf"])


class RenameRequest(BaseModel):
    """Rename the entry at ``path`` to ``new_name`` within the same folder."""

    path: AbsolutePath
    new_name: str = Field(min_length=1, pattern=r"^[^/]+$", examples=["summary.pdf"])


class MoveRequest(BaseModel):
    """Move the entry at ``from_path`` to the full destination path ``to_path``."""

    from_path: AbsolutePath
    to_path: AbsolutePath = Field(examples=["/Archive/report.pdf"])
