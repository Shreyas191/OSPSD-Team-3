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

import cloud_storage_client_api
import dropbox
from dropbox import files
from dropbox.files import SearchOptions

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
        """Upload a local file to Dropbox. TODO(Zesan): implement with ``files_upload``."""
        raise NotImplementedError

    def create_folder(self, remote_path: str) -> cloud_storage_client_api.File:
        """Create a folder. TODO(Zesan): implement with ``files_create_folder_v2``."""
        raise NotImplementedError

    def copy_file(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Duplicate a file. TODO(Zesan): implement with ``files_copy_v2``."""
        raise NotImplementedError

    def copy_folder(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Duplicate a folder. TODO(Zesan): implement with ``files_copy_v2``."""
        raise NotImplementedError

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
            ValueError: If ``new_name`` is empty or contains a ``/``.
            FileNotFoundError: If nothing exists at ``remote_path``.
            FileExistsError: If the parent folder already has an entry named ``new_name``.

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

        Raises:
            FileNotFoundError: If nothing exists at ``from_path``.
            FileExistsError: If something already exists at ``to_path``.

        """
        try:
            result = self.dbx.files_move_v2(from_path, to_path, autorename=False)
        except dropbox.exceptions.ApiError as e:
            translated = _translate_relocation_error(e.error, from_path, to_path)
            if translated is None:
                raise
            raise translated from e
        self.logger.info("Moved %s -> %s", from_path, to_path)
        return DropboxFile(result.metadata)

    # ----- Delete (John) -----

    def delete(self, remote_path: str) -> bool:
        """Delete a file or folder. TODO(John): implement with ``files_delete_v2``."""
        raise NotImplementedError


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


def _translate_relocation_error(reason: object, from_path: str, to_path: str) -> Exception | None:
    """Map a Dropbox ``RelocationError`` onto the built-in exceptions the contract documents.

    Returns ``None`` for errors without a domain meaning (quota, permissions, ...), which
    callers should re-raise unchanged.
    """
    if not isinstance(reason, files.RelocationError):
        return None
    if reason.is_from_lookup() and reason.get_from_lookup().is_not_found():
        return FileNotFoundError(f"No file or folder at {from_path!r}")
    # Dropbox reports moving an entry onto its own path as ``duplicated_or_nested_paths``;
    # the destination is occupied (by the source itself), so it is the same conflict.
    if (reason.is_to() and reason.get_to().is_conflict()) or reason.is_duplicated_or_nested_paths():
        return FileExistsError(f"Something already exists at {to_path!r}")
    return None


def get_client_impl() -> cloud_storage_client_api.Client:
    """Return a configured :class:`DropboxClient` instance."""
    return DropboxClient()


def register() -> None:
    """Register the Dropbox client implementation with the cloud storage client API."""
    cloud_storage_client_api.get_client = get_client_impl
