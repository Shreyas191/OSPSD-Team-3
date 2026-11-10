"""The HTTP routes. Each one calls one ``Client`` method and returns its result.

Every route is on a router with :class:`SharedErrorRoute`, so the exceptions the client
raises become ``{"error": {"code", "message"}}`` responses (see ``errors.py``).
"""

import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from cloud_storage_service.dependencies import StorageClient
from cloud_storage_service.errors import ErrorResponse, SharedErrorRoute
from cloud_storage_service.models import (
    ABSOLUTE_PATH_PATTERN,
    AbsolutePath,
    CopyRequest,
    CreateFolderRequest,
    FileResponse,
    MoveRequest,
    RenameRequest,
)

router = APIRouter(route_class=SharedErrorRoute)


_PROVIDER_ERRORS = {
    502: "The storage provider failed",
    503: "The storage provider is unreachable or busy",
}


def error_responses(descriptions: dict[int, str]) -> dict[int | str, dict[str, Any]]:
    """Document a route's error responses for OpenAPI.

    ``descriptions`` maps each status the route can return to what it means there. The
    502 and 503 provider failures apply to every route, so they are always added.
    """
    documented = {**descriptions, **_PROVIDER_ERRORS}
    return {
        code: {"model": ErrorResponse, "description": text} for code, text in documented.items()
    }


# ----- Create -----


@router.post(
    "/files",
    responses=error_responses(
        {
            403: "The provider does not allow writing there",
            409: "A file or folder is at the specified path or `overwrite` is false",
            422: "Missing file, invalid path, or a file over the provider's upload limit",
        },
    ),
)
def upload_file(
    file: Annotated[UploadFile, File(description="The file contents")],
    remote_path: Annotated[
        str,
        Form(
            pattern=_ABSOLUTE_PATH_PATTERN,
            description="Destination path in cloud storage, including the file name",
            examples=["/Docs/report.pdf"],
        ),
    ],
    client: StorageClient,
) -> FileResponse:
    """Upload a file that does not exist on cloud storage to cloud storage and return its 
    metadata."""
    # The fields are declared one by one rather than as a single form model: FastAPI declares
    # a form model that contains an ``UploadFile`` as ``application/x-www-form-urlencoded`` in
    # the OpenAPI schema, so Swagger UI's "Try it out" could not send the file.
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "upload"
        with tmp_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        uploaded = client.upload_file(str(tmp_path), remote_path, overwrite=False)
    return FileResponse.from_file(uploaded)

@router.put(
    "/files",
    responses=error_responses(
        {
            403: "The provider does not allow writing there",
            404: "File to be replaced does not exist",
            409: "The file has changed since you last retrieved it. Fetch the latest version "
                "before retrying the replacement.",
            422: "Missing file, invalid path, or a file over the provider's upload limit",
        },
    )
)
def replace_file(
    file: Annotated[UploadFile, File(description="Replacement file contents")],
    remote_path: Annotated[
        str,
        Form(
            pattern=_ABSOLUTE_PATH_PATTERN,
            description="Path of the file to replace, including the file name",
            examples=["/Docs/report.pdf"],
        ),
    ],
    client: StorageClient,
) -> FileResponse:
    """Replace a file at the destination"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "upload"
        with tmp_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        uploaded = client.upload_file(str(tmp_path), remote_path, overwrite=True)
    return FileResponse.from_file(uploaded)

@router.post(
    "/folders/create",
    responses=error_responses(
        {
            403: "The provider does not allow creating it there",
            409: "Something is already at the path",
            422: "Missing, relative or root path",
        },
    ),
)
def create_folder(request: CreateFolderRequest, client: StorageClient) -> FileResponse:
    """Create a new folder at the specified path and return its metadata."""
    return FileResponse.from_file(client.create_folder(request.remote_path))


@router.post(
    "/files/copy",
    responses=error_responses(
        {
            403: "The provider does not allow the copy",
            404: "Nothing exists at the source path",
            409: "The destination is taken (including by the source itself)",
            422: "Invalid path, or a folder copied into itself",
        },
    ),
)
def copy_entry(request: CopyRequest, client: StorageClient) -> FileResponse:
    """Copy a file or folder (with its contents) to a new path and return the copy."""
    return FileResponse.from_file(client.copy(request.from_path, request.to_path))


# ----- Read -----


@router.get(
    "/files/metadata",
    responses=error_responses(
        {
            403: "The provider does not allow reading metadata",
            404: "Nothing exists at the path",
            422: "Missing, relative or root path",
        },
    ),
)
def get_file_metadata(path: AbsolutePath, client: StorageClient) -> FileResponse:
    """Get metadata for a single file or folder by absolute path."""
    return FileResponse.from_file(client.get_metadata(path))


@router.get(
    "/files/download",
    response_class=Response,
    responses=error_responses(
        {
            403: "The provider does not allow reading the file",
            404: "Nothing exists at the path",
            422: "Path points to a folder, or invalid path",
        },
    ),
)
def download_file(path: AbsolutePath, client: StorageClient) -> Response:
    """Download the raw bytes of the file located at ``path``."""
    with tempfile.TemporaryDirectory() as temp_dir:
        local_path = Path(temp_dir) / "downloaded_file"
        client.download_file(path, str(local_path))
        file_bytes = local_path.read_bytes()
    return Response(content=file_bytes, media_type="application/octet-stream")


@router.get(
    "/folders/list",
    responses=error_responses(
        {
            403: "The provider does not allow listing the folder",
            404: "Folder does not exist",
            422: "Path points to a file, or invalid relative path",
        },
    ),
)
def list_folder(
    client: StorageClient,
    path: Annotated[str, Query(pattern=r"^(/.*)?$")] = "",
) -> list[FileResponse]:
    """List all direct children in the folder at ``path``."""
    return [FileResponse.from_file(entry) for entry in client.list_folder(path)]


@router.get("/files/search", responses=error_responses({422: "Invalid query parameters"}))
def search_files(
    q: Annotated[str, Query(min_length=1, max_length=1000)],
    client: StorageClient,
    max_results: Annotated[int, Query(gt=0, le=1000)] = 10,
) -> list[FileResponse]:
    """Search for files and folders matching a query string."""
    return [FileResponse.from_file(result) for result in client.search(q, max_results=max_results)]


# ----- Update -----

_UPDATE_ERRORS = error_responses(
    {
        403: "The provider does not allow the change",
        404: "Nothing exists at the source path",
        409: "The destination is taken (including by the source itself)",
        422: "Invalid path or name, or a folder moved into itself",
    },
)


@router.post("/files/rename", responses=_UPDATE_ERRORS)
def rename_file(request: RenameRequest, client: StorageClient) -> FileResponse:
    """Rename a file or folder in place and return its updated metadata."""
    return FileResponse.from_file(client.rename(request.path, request.new_name))


@router.post("/files/move", responses=_UPDATE_ERRORS)
def move_file(request: MoveRequest, client: StorageClient) -> FileResponse:
    """Move a file or folder to a new path and return its updated metadata."""
    return FileResponse.from_file(client.move(request.from_path, request.to_path))


# ----- Delete -----


@router.delete(
    "/files",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(
        {
            403: "The provider does not allow deleting the entry",
            404: "Nothing at the path (including one already deleted)",
            422: "Missing, relative or root path",
        },
    ),
)
def delete_entry(path: AbsolutePath, client: StorageClient) -> None:
    """Delete a file, or a folder with its contents, at the absolute ``path``."""
    client.delete(path)
