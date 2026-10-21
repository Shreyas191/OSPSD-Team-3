"""Dropbox Client Implementation.

This module provides a concrete implementation of the cloud storage client API using
the official Dropbox Python SDK.

Credentials come from :func:`dropbox_client_impl.auth.get_dropbox_client`; this module
never reads tokens itself, so the auth mechanism can change without touching CRUD code.

Each method below is a stub owned by a team member. Replace the ``NotImplementedError``
with a real Dropbox call on your own ``<name>-<feature>`` branch.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import cloud_storage_client_api
import dropbox
import requests
from dropbox import files, stone_validators
from dropbox.files import SearchOptions, WriteMode

from dropbox_client_impl.auth import get_dropbox_client
from dropbox_client_impl.file_impl import DropboxFile

_SEARCH_MAX_RESULTS_LIMIT = 1000


class DropboxClient(cloud_storage_client_api.Client):
    """Concrete implementation of the Client abstraction using the Dropbox API."""

    def __init__(self, dbx: dropbox.Dropbox | None = None) -> None:
        """Initialize the DropboxClient.

        Args:
            dbx: An already-configured ``dropbox.Dropbox`` instance. When provided,
                authentication is skipped (useful for tests).

        Raises:
            DropboxAuthError: If no instance is provided and Dropbox is not authorized.

        """
        self.logger = logging.getLogger(__name__)
        self.dbx = dbx if dbx is not None else get_dropbox_client()

    # ----- Create (Zesan) -----
    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = False,
    ) -> cloud_storage_client_api.File:
        """Upload a local file to Dropbox with ``files_upload``.

        Dropbox's autorename is disabled, so an existing file is either replaced
        (``overwrite=True``) or reported as a conflict. Every Dropbox failure is translated
        (see :func:`_translate_write_error`), so no SDK exception reaches the caller.

        Raises:
            FileNotFoundError: If ``local_path`` does not exist (Dropbox is not contacted).
            FileExistsError: If something already exists at ``remote_path`` and
                ``overwrite`` is false, or a folder is there.
            ValueError: If Dropbox rejects ``remote_path`` as malformed or the file is too
                large for a single upload (150 MB).
            PermissionError: If Dropbox does not allow writing to ``remote_path``.
            ConnectionError: If Dropbox could not be reached or rate limited the request.
            OSError: For any other Dropbox failure, such as a full account.

        """
        mode = WriteMode.overwrite if overwrite else WriteMode.add

        with Path(local_path).open("rb") as source:
            try:
                metadata = self.dbx.files_upload(
                    source.read(),
                    remote_path,
                    mode=mode,
                    autorename=False,
                    strict_conflict=not overwrite,
                )
            except _DROPBOX_FAILURES as e:
                raise _translate_write_error(e, "upload", remote_path) from e

        return DropboxFile(metadata)

    def create_folder(self, remote_path: str) -> cloud_storage_client_api.File:
        """Create a folder (and any missing parents) with ``files_create_folder_v2``.

        Every Dropbox failure is translated (see :func:`_translate_write_error`), so no SDK
        exception reaches the caller.

        Raises:
            FileExistsError: If something already exists at ``remote_path``.
            ValueError: If Dropbox rejects ``remote_path`` as malformed.
            PermissionError: If Dropbox does not allow creating a folder there.
            ConnectionError: If Dropbox could not be reached or rate limited the request.
            OSError: For any other Dropbox failure, such as a full account.

        """
        try:
            res = self.dbx.files_create_folder_v2(remote_path)
        except _DROPBOX_FAILURES as e:
            raise _translate_write_error(e, "create_folder", remote_path) from e
        return DropboxFile(res.metadata)

    def copy(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Copy a file or folder to ``to_path`` with ``files_copy_v2``.

        Dropbox copies files and folders with the same call, so one method covers both.
        Autorename is disabled, so on success the copy's path is exactly ``to_path``.

        Every Dropbox failure is translated (see :func:`_translate_relocation_error`), so no
        SDK exception reaches the caller.

        Raises:
            ValueError: If ``to_path`` is inside ``from_path`` (a folder copied into itself;
                Dropbox is not contacted), or Dropbox rejects a path as malformed.
            FileNotFoundError: If nothing exists at ``from_path``.
            FileExistsError: If something already exists at ``to_path``, including the
                source itself.
            PermissionError: If Dropbox does not allow the copy (no write permission, or
                shared-folder restrictions).
            ConnectionError: If Dropbox could not be reached or rate limited the request.
            OSError: For any other Dropbox failure, such as a full account.

        """
        _reject_nested_destination("copy", from_path, to_path)
        try:
            result = self.dbx.files_copy_v2(from_path, to_path, autorename=False)
        except _DROPBOX_FAILURES as e:
            raise _translate_relocation_error(e, "copy", from_path, to_path) from e
        return DropboxFile(result.metadata)

    # ----- Read (Jing) -----

    def download_file(self, remote_path: str, local_path: str) -> cloud_storage_client_api.File:
        """Download a file from remote_path to local_path.

        Inputs:
            remote_path: Absolute path of the remote file on Dropbox.
            local_path: Local filesystem path where the file contents should be written.

        Returns:
            The metadata of the downloaded file as a :class:`DropboxFile`.

        Side effects:
            Writes or overwrites the local file at ``local_path``.
            Performs a read request on Dropbox.

        Caller assumptions:
            Parent directories for ``local_path`` exist and are writable. ``remote_path`` points
            to a file, not a directory.

        Raises:
            FileNotFoundError: If nothing exists at ``remote_path``.
            IsADirectoryError: If ``remote_path`` is a folder.

        """
        try:
            metadata = self.dbx.files_download_to_file(
                download_path=local_path,
                path=remote_path,
            )
        except dropbox.exceptions.ApiError as e:
            translated = _translate_download_error(e.error, remote_path)
            if translated is None:
                raise
            raise translated from e
        return DropboxFile(metadata)

    def get_metadata(self, remote_path: str) -> cloud_storage_client_api.File:
        """Get file or folder metadata.

        Inputs:
            remote_path: Absolute path of the file or folder on Dropbox.

        Returns:
            A :class:`DropboxFile` instance representing the metadata.

        Side effects:
            None. Read-only query against Dropbox.

        Caller assumptions:
            ``remote_path`` is a non-empty absolute path starting with ``/``.

        Raises:
            FileNotFoundError: If nothing exists at ``remote_path``.

        """
        try:
            metadata = self.dbx.files_get_metadata(remote_path)
        except dropbox.exceptions.ApiError as e:
            translated = _translate_get_metadata_error(e.error, remote_path)
            if translated is None:
                raise
            raise translated from e
        return DropboxFile(metadata)

    def list_folder(self, remote_path: str = "") -> Iterator[cloud_storage_client_api.File]:
        """List direct children in a folder, paginating until complete.

        Inputs:
            remote_path: Absolute path of the folder to list (or ``""`` for root).

        Returns:
            An iterator yielding a :class:`DropboxFile` for each child entry.

        Side effects:
            None. Read-only query against Dropbox.

        Caller assumptions:
            ``remote_path`` points to an existing folder (or is empty string for the root).

        Raises:
            FileNotFoundError: If the folder does not exist.
            ValueError: If ``remote_path`` is a file rather than a folder.

        """
        try:
            result = self.dbx.files_list_folder(remote_path)
        except dropbox.exceptions.ApiError as e:
            translated = _translate_list_folder_error(e.error, remote_path)
            if translated is None:
                raise
            raise translated from e

        for entry in result.entries:
            yield DropboxFile(entry)

        while result.has_more:
            try:
                result = self.dbx.files_list_folder_continue(result.cursor)
            except dropbox.exceptions.ApiError as e:
                translated = _translate_list_folder_continue_error(e.error, remote_path)
                if translated is None:
                    raise
                raise translated from e
            for entry in result.entries:
                yield DropboxFile(entry)

    def search(self, query: str, max_results: int = 10) -> Iterator[cloud_storage_client_api.File]:
        """Search for files and folders by name.

        Inputs:
            query: Non-empty search string to match against entry names.
            max_results: Maximum number of entries to yield (must be between 1 and 1000,
                defaults to 10).

        Returns:
            An iterator yielding up to ``max_results`` matching :class:`DropboxFile` instances.

        Side effects:
            None. Read-only query against Dropbox.

        Caller assumptions:
            ``query`` is non-empty and ``max_results`` is an integer between 1 and 1000.

        Raises:
            ValueError: If ``query`` is empty or ``max_results`` is not between 1 and 1000.

        """
        if not query:
            msg = "Search query must be non-empty"
            raise ValueError(msg)
        if max_results <= 0 or max_results > _SEARCH_MAX_RESULTS_LIMIT:
            msg = f"max_results must be greater than 0 and at most 1000, got {max_results}"
            raise ValueError(msg)

        options = SearchOptions(max_results=max_results, filename_only=True)
        result = self.dbx.files_search_v2(query=query, options=options)
        yielded = 0

        while True:
            for match in result.matches:
                if match.metadata.is_metadata():
                    meta = match.metadata.get_metadata()
                    if isinstance(meta, files.FileMetadata | files.FolderMetadata):
                        yield DropboxFile(meta)
                        yielded += 1
                        if yielded >= max_results:
                            return

            if not result.has_more:
                break

            result = self.dbx.files_search_continue_v2(result.cursor)

    # ----- Update (Shreyas) -----

    def rename(self, remote_path: str, new_name: str) -> cloud_storage_client_api.File:
        """Rename a file or folder in place using ``files_move_v2``.

        Dropbox has no dedicated rename call, so this is a move to a sibling path in the
        same parent folder.

        Raises:
            ValueError: If ``new_name`` is empty or contains a ``/`` (Dropbox is not
                contacted), or Dropbox rejects the path.
            FileNotFoundError: If nothing exists at ``remote_path``.
            FileExistsError: If the parent folder already has an entry named ``new_name``.
            PermissionError, ConnectionError, OSError: As for :meth:`move`.

        """
        if not new_name or "/" in new_name:
            msg = f"Invalid name {new_name!r}: must be non-empty and must not contain '/'"
            raise ValueError(msg)
        parent = remote_path.rstrip("/").rpartition("/")[0]
        return self.move(remote_path, f"{parent}/{new_name}")

    def move(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Move a file or folder to ``to_path`` using ``files_move_v2``.

        ``to_path`` is the full destination path, including the entry's name. Existing
        entries are never overwritten and Dropbox's autorename is disabled, so on success
        the returned file's path is exactly ``to_path``.

        Dropbox paths are case-insensitive, so a destination that differs from the source
        only in letter case (e.g. ``report.txt`` -> ``Report.txt``) counts as occupied.

        Every Dropbox failure is translated (see :func:`_translate_relocation_error`), so no
        SDK exception reaches the caller.

        Raises:
            ValueError: If ``to_path`` is inside ``from_path`` (a folder moved into itself;
                Dropbox is not contacted), or Dropbox rejects a path as malformed.
            FileNotFoundError: If nothing exists at ``from_path``.
            FileExistsError: If something already exists at ``to_path``.
            PermissionError: If Dropbox does not allow the move (no write permission,
                locked or shared content, or a restricted destination).
            ConnectionError: If Dropbox could not be reached or rate limited the request.
            OSError: For any other Dropbox failure, such as a full account.

        """
        _reject_nested_destination("move", from_path, to_path)
        try:
            result = self.dbx.files_move_v2(from_path, to_path, autorename=False)
        except _DROPBOX_FAILURES as e:
            raise _translate_relocation_error(e, "move", from_path, to_path) from e
        self.logger.info("Moved %s -> %s", from_path, to_path)
        return DropboxFile(result.metadata)

    # ----- Delete (John) -----

    def delete(self, remote_path: str) -> bool:
        """Delete a file or folder (with its contents) using ``files_delete_v2``.

        Dropbox moves the entry to the account's trash, from where it can be restored in
        the Dropbox web app; this client does not expose restore.

        Every Dropbox failure is translated (see :func:`_translate_delete_error`), so no
        SDK exception reaches the caller.

        Raises:
            ValueError: If ``remote_path`` is empty or the root (Dropbox is not contacted),
                or Dropbox rejects it as malformed.
            FileNotFoundError: If nothing exists at ``remote_path``.
            PermissionError: If Dropbox does not allow deleting the entry.
            ConnectionError: If Dropbox could not be reached, rate limited the request,
                or had too many write operations in progress.
            OSError: For any other Dropbox failure.

        """
        if not remote_path.strip("/"):
            msg = f"Refusing to delete the root folder (path {remote_path!r})"
            raise ValueError(msg)
        try:
            self.dbx.files_delete_v2(remote_path)
        except (
            dropbox.exceptions.DropboxException,
            stone_validators.ValidationError,
            requests.exceptions.RequestException,
        ) as e:
            raise _translate_delete_error(e, remote_path) from e
        self.logger.info("Deleted %s", remote_path)
        return True


def _translate_download_error(reason: object, remote_path: str) -> Exception | None:
    """Map a Dropbox ``DownloadError`` onto the built-in exceptions the contract documents.

    Returns ``None`` for errors without a domain meaning, which callers should re-raise.
    """
    if not isinstance(reason, files.DownloadError):
        return None
    if reason.is_path():
        lookup = reason.get_path()
        if lookup.is_not_found():
            return FileNotFoundError(f"No file at {remote_path!r}")
        if lookup.is_not_file():
            return IsADirectoryError(f"Path is a folder: {remote_path!r}")
    return None


def _translate_get_metadata_error(reason: object, remote_path: str) -> Exception | None:
    """Map a Dropbox ``GetMetadataError`` onto the built-in exceptions the contract documents.

    Returns ``None`` for errors without a domain meaning, which callers should re-raise.
    """
    if not isinstance(reason, files.GetMetadataError):
        return None
    if reason.is_path() and reason.get_path().is_not_found():
        return FileNotFoundError(f"No file or folder at {remote_path!r}")
    return None


def _translate_list_folder_error(reason: object, remote_path: str) -> Exception | None:
    """Map a Dropbox ``ListFolderError`` onto the built-in exceptions the contract documents.

    Returns ``None`` for errors without a domain meaning, which callers should re-raise.
    """
    if not isinstance(reason, files.ListFolderError):
        return None
    if reason.is_path():
        lookup = reason.get_path()
        if lookup.is_not_found():
            return FileNotFoundError(f"No folder at {remote_path!r}")
        if lookup.is_not_folder():
            return ValueError(f"Path is a file: {remote_path!r}")
    return None


def _translate_list_folder_continue_error(reason: object, remote_path: str) -> Exception | None:
    """Map a ``ListFolderContinueError`` onto the built-in exceptions the contract documents.

    Returns ``None`` for errors without a domain meaning, which callers should re-raise.
    """
    if not isinstance(reason, files.ListFolderContinueError):
        return None
    if reason.is_path() and reason.get_path().is_not_found():
        return FileNotFoundError(f"No folder at {remote_path!r}")
    return None


# Every failure an SDK call can raise: Dropbox API and HTTP errors, the SDK's local
# validation of arguments (e.g. a path without a leading "/"), and network errors.
_DROPBOX_FAILURES = (
    dropbox.exceptions.DropboxException,
    stone_validators.ValidationError,
    requests.exceptions.RequestException,
)


def _sdk_failure_kind(error: Exception) -> type[Exception] | None:
    """Return the exception for failures that mean the same for every call, else ``None``."""
    # The SDK checks the path format locally before sending, e.g. a path without a leading "/".
    if isinstance(error, stone_validators.ValidationError):
        return ValueError
    if isinstance(error, dropbox.exceptions.RateLimitError | requests.exceptions.RequestException):
        return ConnectionError
    return None


def _failure_detail(error: Exception) -> object:
    """Return the Dropbox error tag, or the SDK exception's class name; neither holds secrets."""
    return error.error if isinstance(error, dropbox.exceptions.ApiError) else type(error).__name__


def _reject_nested_destination(operation: str, from_path: str, to_path: str) -> None:
    """Raise ``ValueError`` if ``to_path`` is inside ``from_path``, without contacting Dropbox.

    Dropbox reports a folder moved or copied into itself with the same
    ``duplicated_or_nested_paths`` error as an operation onto the source's own path, so the
    two can only be told apart here. Paths are compared case-insensitively, as Dropbox does.
    """
    if to_path.lower().startswith(f"{from_path.lower().rstrip('/')}/"):
        msg = f"Cannot {operation} {from_path!r} into itself: {to_path!r} is inside the source"
        raise ValueError(msg)


# ----- Move and copy (files_move_v2 / files_copy_v2) -----

# Message for each exception ``Client.move`` and ``Client.copy`` document. As for delete,
# only provider failures carry the Dropbox error ``detail``; the service never shows those to
# callers. ``{doing}`` and ``{done}`` come from ``_RELOCATION_VERBS``.
_RELOCATION_FAILURE_MESSAGES: dict[type[Exception], str] = {
    FileNotFoundError: "No file or folder at {from_path!r}",
    FileExistsError: "Something already exists at {to_path!r}",
    ValueError: "Dropbox rejected {doing} {from_path!r} to {to_path!r} as an invalid path",
    PermissionError: "Dropbox does not allow {doing} {from_path!r} to {to_path!r}",
    ConnectionError: "Dropbox is unreachable or busy, so {from_path!r} was not {done} ({detail})",
    OSError: "Dropbox failed to {operation} {from_path!r} to {to_path!r} ({detail})",
}
_RELOCATION_VERBS = {"move": ("moving", "moved"), "copy": ("copying", "copied")}


def _translate_relocation_error(
    error: Exception,
    operation: str,
    from_path: str,
    to_path: str,
) -> Exception:
    """Map a failed ``files_move_v2`` or ``files_copy_v2`` call onto the documented exceptions."""
    kind = _relocation_failure_kind(error)
    doing, done = _RELOCATION_VERBS[operation]
    message = _RELOCATION_FAILURE_MESSAGES[kind].format(
        operation=operation,
        doing=doing,
        done=done,
        from_path=from_path,
        to_path=to_path,
        detail=_failure_detail(error),
    )
    return kind(message)


def _relocation_failure_kind(error: Exception) -> type[Exception]:  # noqa: PLR0911
    """Pick the exception for a failed move or copy; ``OSError`` when nothing more specific fits."""
    sdk_kind = _sdk_failure_kind(error)
    if sdk_kind is not None:
        return sdk_kind
    reason = error.error if isinstance(error, dropbox.exceptions.ApiError) else None
    if not isinstance(reason, files.RelocationError):
        # Other SDK errors (auth, 5xx, bad input) and unknown error types.
        return OSError
    if reason.is_from_lookup():
        return _lookup_failure_kind(reason.get_from_lookup()) or OSError
    if reason.is_from_write():
        return _write_failure_kind(reason.get_from_write()) or OSError
    if reason.is_to():
        to = reason.get_to()
        return FileExistsError if to.is_conflict() else _write_failure_kind(to) or OSError
    # ``move`` and ``copy`` reject nested destinations before calling Dropbox, so this means
    # the destination is the source itself: it is occupied, the same conflict as above.
    if reason.is_duplicated_or_nested_paths():
        return FileExistsError
    if reason.is_cant_move_folder_into_itself():
        return ValueError
    if (
        reason.is_cant_copy_shared_folder()
        or reason.is_cant_nest_shared_folder()
        or reason.is_cant_move_shared_folder()
        or reason.is_cant_move_into_vault()
        or reason.is_cant_move_into_family()
        or reason.is_cant_transfer_ownership()
    ):
        return PermissionError
    # Quota (insufficient_quota and its team/member variants), too_many_files,
    # internal_error and unknown tags.
    return OSError


# ----- Upload and create folder (files_upload / files_create_folder_v2) -----

# Message for each exception ``Client.upload_file`` and ``Client.create_folder`` document.
# ``{doing}`` and ``{done}`` come from ``_WRITE_VERBS``.
_WRITE_FAILURE_MESSAGES: dict[type[Exception], str] = {
    FileExistsError: "Something already exists at {path!r}",
    ValueError: "Dropbox rejected {path!r} as an invalid path",
    PermissionError: "Dropbox does not allow {doing} {path!r}",
    ConnectionError: "Dropbox is unreachable or busy, so {path!r} was not {done} ({detail})",
    OSError: "Dropbox failed {doing} {path!r} ({detail})",
}
_WRITE_VERBS = {
    "upload": ("uploading to", "uploaded"),
    "create_folder": ("creating a folder at", "created"),
}
_UPLOAD_TOO_LARGE_MESSAGE = (
    "The file for {path!r} is too large to upload in one request (Dropbox's limit is 150 MB)"
)


def _translate_write_error(error: Exception, operation: str, path: str) -> Exception:
    """Map a failed ``files_upload`` or ``files_create_folder_v2`` to the documented exceptions."""
    reason = error.error if isinstance(error, dropbox.exceptions.ApiError) else None
    if isinstance(reason, files.UploadError) and reason.is_payload_too_large():
        return ValueError(_UPLOAD_TOO_LARGE_MESSAGE.format(path=path))
    kind = _write_operation_failure_kind(error)
    doing, done = _WRITE_VERBS[operation]
    message = _WRITE_FAILURE_MESSAGES[kind].format(
        doing=doing,
        done=done,
        path=path,
        detail=_failure_detail(error),
    )
    return kind(message)


def _write_operation_failure_kind(error: Exception) -> type[Exception]:
    """Pick the exception for a failed upload or folder creation; ``OSError`` if nothing fits."""
    sdk_kind = _sdk_failure_kind(error)
    if sdk_kind is not None:
        return sdk_kind
    reason = error.error if isinstance(error, dropbox.exceptions.ApiError) else None
    # UploadError.path wraps the WriteError in UploadWriteFailed.reason; CreateFolderError.path
    # is the WriteError itself.
    write: files.WriteError | None = None
    if isinstance(reason, files.UploadError) and reason.is_path():
        write = reason.get_path().reason
    elif isinstance(reason, files.CreateFolderError) and reason.is_path():
        write = reason.get_path()
    if write is None:
        # Other SDK errors (auth, 5xx, bad input), content_hash_mismatch, unknown tags.
        return OSError
    if write.is_conflict():
        return FileExistsError
    return _write_failure_kind(write) or OSError


# Message for each exception ``Client.delete`` documents. Messages of the 4xx kinds are
# shown to API callers, so only provider failures carry the Dropbox error ``detail``.
_DELETE_FAILURE_MESSAGES: dict[type[Exception], str] = {
    FileNotFoundError: "No file or folder at {path!r}",
    ValueError: "Dropbox rejected {path!r} as an invalid path",
    PermissionError: "Dropbox does not allow deleting {path!r}",
    ConnectionError: "Dropbox is unreachable or busy, so {path!r} was not deleted ({detail})",
    OSError: "Dropbox failed to delete {path!r} ({detail})",
}


def _translate_delete_error(error: Exception, remote_path: str) -> Exception:
    """Map a failed ``files_delete_v2`` call onto the exceptions ``Client.delete`` documents.

    The ``detail`` kept for provider failures is the Dropbox error tag or the SDK exception's
    class name; neither can contain credentials.
    """
    kind = _delete_failure_kind(error)
    detail = _failure_detail(error)
    return kind(_DELETE_FAILURE_MESSAGES[kind].format(path=remote_path, detail=detail))


def _delete_failure_kind(error: Exception) -> type[Exception]:
    """Pick the exception for a failed delete; ``OSError`` when nothing more specific fits."""
    # The SDK checks the path format locally before sending, e.g. a path without a leading "/".
    if isinstance(error, stone_validators.ValidationError):
        return ValueError
    if isinstance(error, dropbox.exceptions.RateLimitError | requests.exceptions.RequestException):
        return ConnectionError
    reason = error.error if isinstance(error, dropbox.exceptions.ApiError) else None
    if isinstance(reason, files.DeleteError):
        if reason.is_too_many_write_operations():
            return ConnectionError
        if reason.is_path_lookup():
            return _lookup_failure_kind(reason.get_path_lookup()) or OSError
        if reason.is_path_write():
            return _write_failure_kind(reason.get_path_write()) or OSError
    # Other SDK errors (auth, 5xx, bad input), ``too_many_files`` (the folder is too large to
    # delete in one call) and unknown tags.
    return OSError


def _lookup_failure_kind(lookup: files.LookupError) -> type[Exception] | None:
    """Return the exception for a lookup failure (delete or move), or ``None`` if none fits."""
    if lookup.is_not_found():
        return FileNotFoundError
    if lookup.is_malformed_path():
        return ValueError
    if lookup.is_restricted_content() or lookup.is_locked():
        return PermissionError
    return None


def _write_failure_kind(write: files.WriteError) -> type[Exception] | None:
    """Return the exception for a write failure (delete or move), or ``None`` if none fits."""
    if write.is_malformed_path() or write.is_disallowed_name():
        return ValueError
    if (
        write.is_no_write_permission()
        or write.is_team_folder()
        or write.is_access_restricted()
        or write.is_operation_suppressed()
    ):
        return PermissionError
    if write.is_too_many_write_operations():
        return ConnectionError
    return None


def get_client_impl() -> cloud_storage_client_api.Client:
    """Return a configured :class:`DropboxClient` instance."""
    return DropboxClient()


def register() -> None:
    """Register the Dropbox client implementation with the cloud storage client API."""
    cloud_storage_client_api.get_client = get_client_impl
