"""FastAPI application exposing cloud storage operations over HTTP.

Request flow: FastAPI validates the JSON body against a request model, the route calls the
provider-independent ``cloud_storage_client_api.Client``, and the returned ``File`` is
serialized as a :class:`FileResponse`. Routes never see Dropbox types: the client raises
built-in exceptions (``FileNotFoundError``, ``FileExistsError``, ``ValueError``), which are
mapped to HTTP status codes here.

Paths are passed in the request body rather than the URL because they contain ``/``.

Provider wiring: :func:`build_storage_client` is the only place the service chooses a
concrete provider. The app builds that client once at startup, keeps it on ``app.state``,
and routes receive it through the :func:`get_storage_client` dependency, so they only ever
see the abstract ``Client``. Tests replace it with
``app.dependency_overrides[get_storage_client]``.
"""

import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import cloud_storage_client_api
from dropbox_client_impl import DropboxClient
from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

AbsolutePath = Annotated[str, Field(pattern=r"^/.+", examples=["/Docs/report.pdf"])]


def build_storage_client() -> cloud_storage_client_api.Client:
    """Build the storage client the service runs against.

    This is where the provider is selected: to switch providers, return a different
    ``Client`` implementation here. Building a ``DropboxClient`` makes no network call, but
    it raises ``DropboxAuthError`` if no Dropbox credentials are configured, so the service
    fails at startup instead of on its first request.
    """
    return DropboxClient()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the storage client when the app starts; it lives as long as the app."""
    app.state.storage_client = build_storage_client()
    yield


app = FastAPI(title="Cloud Storage Service", lifespan=lifespan)


def get_storage_client(request: Request) -> cloud_storage_client_api.Client:
    """Return the storage client created at startup."""
    client: cloud_storage_client_api.Client = request.app.state.storage_client
    return client


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


class UploadForm(BaseModel):
    """Upload a file as multipart/form-data."""

    file: UploadFile
    remote_path: AbsolutePath = Field(
        description="Destination path in cloud storage",
        examples=["/Docs/report.pdf"],
    )
    overwrite: bool = Field(
        default=False,
        description="Whether to overwrite if the file already exists",
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

_READ_ERROR_RESPONSES: dict[int | str, dict[str, str]] = {
    404: {"description": "The requested file or folder does not exist"},
    422: {"description": "Validation error or invalid target path or target is a folder"},
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


# ----- Create Endpoints -----


@app.post("/files/upload", responses=_ERROR_RESPONSES)
def upload_file(form: Annotated[UploadForm, Form()], client: StorageClient) -> FileResponse:
    """Upload a file to cloud storage and return its metadata."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "upload"
        with tmp_path.open("wb") as out:
            shutil.copyfileobj(form.file.file, out)
        try:
            uploaded = client.upload_file(
                str(tmp_path),
                form.remote_path,
                overwrite=form.overwrite,
            )
        except (FileNotFoundError, FileExistsError, ValueError) as e:
            raise _to_http_error(e) from e
    return FileResponse.from_file(uploaded)


@app.post("/folders/create", responses=_ERROR_RESPONSES)
def create_folder(request: CreateFolderRequest, client: StorageClient) -> FileResponse:
    """Create a new folder at the specified path and return its metadata."""
    try:
        created = client.create_folder(request.remote_path)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        raise _to_http_error(e) from e
    return FileResponse.from_file(created)


@app.post("/files/copy", responses=_ERROR_RESPONSES)
def copy_entry(request: CopyRequest, client: StorageClient) -> FileResponse:
    """Copy a file or folder (with its contents) to a new path and return the copy."""
    try:
        copied = client.copy(request.from_path, request.to_path)
    except (FileNotFoundError, FileExistsError) as e:
        raise _to_http_error(e) from e
    return FileResponse.from_file(copied)


# ----- Read Endpoints -----


@app.get("/files/metadata", responses=_READ_ERROR_RESPONSES)
def get_file_metadata(path: AbsolutePath, client: StorageClient) -> FileResponse:
    """Get metadata for a single file or folder by absolute path."""
    try:
        meta = client.get_metadata(path)
    except FileNotFoundError as e:
        raise _to_http_error(e) from e
    return FileResponse.from_file(meta)


@app.get(
    "/files/download",
    response_class=Response,
    responses=_READ_ERROR_RESPONSES,
)
def download_file(path: AbsolutePath, client: StorageClient) -> Response:
    """Download the raw bytes of the file located at ``path``."""
    with tempfile.TemporaryDirectory() as temp_dir:
        local_path = Path(temp_dir) / "downloaded_file"
        try:
            client.download_file(path, str(local_path))
        except (FileNotFoundError, IsADirectoryError) as e:
            raise _to_http_error(e) from e
        file_bytes = local_path.read_bytes()

    return Response(content=file_bytes, media_type="application/octet-stream")


@app.get("/folders/list", responses=_READ_ERROR_RESPONSES)
def list_folder(
    client: StorageClient,
    path: Annotated[str, Query(pattern=r"^(/.*)?$")] = "",
) -> list[FileResponse]:
    """List all direct children in the folder at ``path``."""
    try:
        entries = list(client.list_folder(path))
    except (FileNotFoundError, ValueError) as e:
        raise _to_http_error(e) from e
    return [FileResponse.from_file(entry) for entry in entries]


@app.get("/files/search", responses=_READ_ERROR_RESPONSES)
def search_files(
    q: Annotated[str, Query(min_length=1)],
    client: StorageClient,
    max_results: Annotated[int, Query(gt=0, le=1000)] = 10,
) -> list[FileResponse]:
    """Search for files and folders matching a query string."""
    try:
        results = list(client.search(q, max_results=max_results))
    except ValueError as e:
        raise _to_http_error(e) from e
    return [FileResponse.from_file(result) for result in results]
