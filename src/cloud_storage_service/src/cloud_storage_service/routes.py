"""The HTTP routes. Each one calls one ``Client`` method and returns its result.

Every route is on a router with :class:`SharedErrorRoute`, so the exceptions the client
raises become ``{"error": {"code", "message"}}`` responses (see ``errors.py``).
"""

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from pydantic import AfterValidator

from cloud_storage_service.dependencies import StorageClient
from cloud_storage_service.errors import ErrorResponse, SharedErrorRoute
from cloud_storage_service.models import (
    CopyRequest,
    CreateFolderRequest,
    EntryPath,
    FileResponse,
    FolderPath,
    ListEntry,
    MoveRequest,
    PatchRequest,
    RenameRequest,
    clean_path,
)

router = APIRouter(route_class=SharedErrorRoute)


_INVALID_REQUEST = {400: "Missing field, invalid path, or a body that is not valid JSON"}
_PROVIDER_ERRORS = {
    502: "The storage provider failed",
    503: "The storage provider is unreachable or busy",
}


def error_responses(descriptions: dict[int, str]) -> dict[int | str, dict[str, Any]]:
    """Document a route's error responses for OpenAPI.

    ``descriptions`` maps each status the route can return to what it means there. A 400
    for an invalid request and the 502 and 503 provider failures apply to every route, so
    they are always added (a route may describe its own 400 instead).
    """
    documented = {**_INVALID_REQUEST, **descriptions, **_PROVIDER_ERRORS}
    return {
        code: {"model": ErrorResponse, "description": text} for code, text in documented.items()
    }


# ----- Create -----


@contextmanager
def saved_upload(file: UploadFile) -> Iterator[str]:
    """Save an uploaded file to a temporary local path, removed when the block ends."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / "upload"
        with tmp_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        yield str(tmp_path)


# The form fields are declared one by one rather than as a single form model: FastAPI
# declares a form model that contains an ``UploadFile`` as
# ``application/x-www-form-urlencoded`` in the OpenAPI schema, so Swagger UI's
# "Try it out" could not send the file.
UploadedFile = Annotated[UploadFile, File(description="The file contents")]
FilePathField = Annotated[
    str,
    AfterValidator(clean_path),
    Form(description="Path of the file in cloud storage, including the file name"),
]


@router.post(
    "/files",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        {
            403: "The provider does not allow writing there",
            409: "Something already exists at the path",
            422: "A file over the provider's upload limit",
        },
    ),
)
def create_file(file: UploadedFile, path: FilePathField, client: StorageClient) -> FileResponse:
    """Create a new file at ``path``; an existing file or folder there is never overwritten."""
    with saved_upload(file) as local_path:
        created = client.upload_file(local_path, path, overwrite=False)
    return FileResponse.from_file(created)


@router.put(
    "/files",
    responses=error_responses(
        {
            403: "The provider does not allow writing there",
            404: "No file exists at the path",
            409: "The file changed while it was being replaced; read it again and retry",
            422: "The path is a folder, or the file is over the provider's upload limit",
        },
    ),
)
def replace_file(file: UploadedFile, path: FilePathField, client: StorageClient) -> FileResponse:
    """Replace the contents of the existing file at ``path``; a missing file is never created."""
    with saved_upload(file) as local_path:
        replaced = client.replace_file(local_path, path)
    return FileResponse.from_file(replaced)


@router.post(
    "/files/upload",
    responses=error_responses(
        {
            403: "The provider does not allow writing there",
            409: "Something is at the path: `overwrite` is false, or it's a folder",
            422: "A file over the provider's upload limit",
        },
    ),
)
def upload_file(
    file: Annotated[UploadFile, File(description="The file contents")],
    remote_path: Annotated[
        str,
        AfterValidator(clean_path),
        Form(
            description="Destination path in cloud storage, including the file name",
            examples=["/Docs/report.pdf"],
        ),
    ],
    client: StorageClient,
    *,
    overwrite: Annotated[
        bool,
        Form(description="Replace an existing file at remote_path"),
    ] = False,
) -> FileResponse:
    """Upload a file to cloud storage and return its metadata.

    ``POST /files`` (create) and ``PUT /files`` (replace) are the strict versions; this
    route stays for existing callers.
    """
    with saved_upload(file) as local_path:
        uploaded = client.upload_file(local_path, remote_path, overwrite=overwrite)
    return FileResponse.from_file(uploaded)


@router.post(
    "/folders/create",
    responses=error_responses(
        {
            403: "The provider does not allow creating it there",
            409: "Something is already at the path",
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
            422: "A folder copied into itself",
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
        },
    ),
)
def get_file_metadata(path: EntryPath, client: StorageClient) -> FileResponse:
    """Get metadata for a single file or folder."""
    return FileResponse.from_file(client.get_metadata(path))


@router.get(
    "/files/download",
    response_class=Response,
    responses=error_responses(
        {
            403: "The provider does not allow reading the file",
            404: "Nothing exists at the path",
            422: "The path is a folder",
        },
    ),
)
def download_file(path: EntryPath, client: StorageClient) -> Response:
    """Download the raw bytes of the file located at ``path``."""
    with tempfile.TemporaryDirectory() as temp_dir:
        local_path = Path(temp_dir) / "downloaded_file"
        client.download_file(path, str(local_path))
        file_bytes = local_path.read_bytes()
    return Response(content=file_bytes, media_type="application/octet-stream")


@router.get(
    "/files",
    responses=error_responses(
        {
            400: "Missing or invalid path",
            403: "The provider does not allow listing the folder",
            404: "Folder does not exist",
            422: "The path is a file, not a folder",
        },
    ),
)
def list_folder(path: FolderPath, client: StorageClient) -> list[ListEntry]:
    """List the direct children of the folder at ``path`` (``/`` is the root), sorted by path.

    Every provider page is fetched, so the whole folder comes back as one array.
    """
    entries = client.list_folder("" if path == "/" else path)
    return sorted((ListEntry.from_file(entry) for entry in entries), key=lambda entry: entry.path)


@router.get("/files/search", responses=error_responses({422: "The provider rejects the query"}))
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
        422: "A folder moved into itself",
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


@router.patch(
    "/files",
    responses=error_responses(
        {
            403: "The provider does not allow the move",
            404: "No file exists at the path",
            409: "Something already exists at `new_path`",
            422: "The path is a folder, or `new_path` is the path itself (ignoring case)",
        },
    ),
)
def update_entry(path: EntryPath, request: PatchRequest, client: StorageClient) -> FileResponse:
    """Move or rename the file at ``path`` to ``new_path`` and return its new metadata.

    Follows the shared conformance suite: only files move (a folder is ``422``), a
    ``new_path`` equal to ``path`` ignoring case is ``422``, and nothing already at
    ``new_path`` is overwritten (``409``). The file keeps its ``id``. The source is read
    first to apply those rules, so a move costs one extra provider call.
    """
    source = client.get_metadata(path)
    if source.is_folder:
        msg = f"Cannot move {path!r}: it is a folder, and only files can be moved"
        raise IsADirectoryError(msg)
    if request.new_path.lower() == path.lower():
        msg = f"Cannot move {path!r} to {request.new_path!r}: it is the same path (ignoring case)"
        raise ValueError(msg)
    return FileResponse.from_file(client.move(path, request.new_path))


# ----- Delete -----


@router.delete(
    "/files",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(
        {
            403: "The provider does not allow deleting the entry",
            400: "Missing or invalid path",
            404: "Nothing at the path (including one already deleted)",
            422: "The path is the root folder, which is never deleted",
        },
    ),
)
def delete_entry(path: FolderPath, client: StorageClient) -> None:
    """Delete a file, or a folder with its contents, at ``path``.

    The root is accepted as a path here so the client can refuse it with
    ``422 invalid_operation``: it is a valid path, but deleting it is never allowed.
    """
    client.delete(path)


# ----- Health -----


@router.get(
    "/health",
    status_code=status.HTTP_200_OK,
)
def check_service_health() -> dict[str, str]:
    """Report that the service is running, without contacting the storage provider."""
    return {"status": "ok"}


@router.get(
    "/health/provider",
    status_code=status.HTTP_200_OK,
    responses=error_responses(
        {
            503: "The storage provider is unreachable or busy",
        },
    ),
)
def check_provider_health(client: StorageClient) -> dict[str, str]:
    """Check that the storage provider is reachable with configured credentials."""
    client.check_health()
    return {"status": "ok"}
