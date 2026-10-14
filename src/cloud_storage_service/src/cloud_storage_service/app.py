"""FastAPI application exposing cloud storage operations over HTTP.

Request flow: FastAPI validates the JSON body against a request model, the route calls the
provider-independent ``cloud_storage_client_api.Client``, and the returned ``File`` is
serialized as a :class:`FileResponse`. Routes never see Dropbox types: the client raises
built-in exceptions (``FileNotFoundError``, ``FileExistsError``, ``ValueError``), which are
mapped to HTTP status codes here.

Paths are passed in the request body rather than the URL because they contain ``/``.
"""

from datetime import datetime
from functools import lru_cache
from typing import Annotated

import cloud_storage_client_api
import dropbox_client_impl  # noqa: F401  (import registers the Dropbox implementation)
from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field

AbsolutePath = Annotated[str, Field(pattern=r"^/.+", examples=["/Docs/report.pdf"])]

app = FastAPI(title="Cloud Storage Service")


@lru_cache(maxsize=1)
def get_storage_client() -> cloud_storage_client_api.Client:
    """Return the shared storage client, built once on first use."""
    return cloud_storage_client_api.get_client()


StorageClient = Annotated[cloud_storage_client_api.Client, Depends(get_storage_client)]


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


class RenameRequest(BaseModel):
    """Rename the entry at ``path`` to ``new_name`` within the same folder."""

    path: AbsolutePath
    new_name: str = Field(min_length=1, pattern=r"^[^/]+$", examples=["summary.pdf"])


class MoveRequest(BaseModel):
    """Move the entry at ``from_path`` to the full destination path ``to_path``."""

    from_path: AbsolutePath
    to_path: AbsolutePath = Field(examples=["/Archive/report.pdf"])


def _to_http_error(error: Exception) -> HTTPException:
    """Map the client contract's documented exceptions onto HTTP status codes."""
    if isinstance(error, FileNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(error))
    if isinstance(error, FileExistsError):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error))


_ERROR_RESPONSES: dict[int | str, dict[str, str]] = {
    404: {"description": "The source file or folder does not exist"},
    409: {"description": "The destination already exists"},
}


@app.post("/files/rename", responses=_ERROR_RESPONSES)
def rename_file(request: RenameRequest, client: StorageClient) -> FileResponse:
    """Rename a file or folder in place and return its updated metadata."""
    try:
        renamed = client.rename(request.path, request.new_name)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        raise _to_http_error(e) from e
    return FileResponse.from_file(renamed)


@app.post("/files/move", responses=_ERROR_RESPONSES)
def move_file(request: MoveRequest, client: StorageClient) -> FileResponse:
    """Move a file or folder to a new path and return its updated metadata."""
    try:
        moved = client.move(request.from_path, request.to_path)
    except (FileNotFoundError, FileExistsError) as e:
        raise _to_http_error(e) from e
    return FileResponse.from_file(moved)
