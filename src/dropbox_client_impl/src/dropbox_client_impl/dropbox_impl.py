"""Dropbox Client Implementation.

This module provides a concrete implementation of the cloud storage client API using
the official Dropbox Python SDK.

Credentials come from :func:`dropbox_client_impl.auth.get_dropbox_client`; this module
never reads tokens itself, so the auth mechanism can change without touching CRUD code.
Every SDK failure is translated by :func:`dropbox_client_impl.errors.translate`, so no
Dropbox exception reaches the caller.
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import cloud_storage_client_api
import dropbox
from dropbox import files
from dropbox.files import SearchOptions, WriteMode, FileMetadata

from dropbox_client_impl import errors
from dropbox_client_impl.auth import get_dropbox_client
from dropbox_client_impl.errors import DROPBOX_FAILURES
from dropbox_client_impl.file_impl import DropboxFile

_SEARCH_MAX_RESULTS_LIMIT = 1000
# Dropbox's limit on the length of a search query (SearchV2Arg.query).
_SEARCH_MAX_QUERY_LENGTH = 1000


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

    # ----- Create -----
    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = False,
    ) -> cloud_storage_client_api.File:
        """Upload a local file to Dropbox with ``files_upload``.

        Dropbox's autorename is disabled, so an existing file is either replaced
        (``overwrite=True``) or reported as a conflict.

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
        with Path(local_path).open("rb") as source:
            mode = WriteMode.add
            if overwrite:
                try:
                    current = self.dbx.files_get_metadata(remote_path)
                except DROPBOX_FAILURES as exc:
                    raise errors.translate(exc, errors.METADATA, remote_path) from exc

                if not isinstance(current, FileMetadata):
                    raise FileNotFoundError(remote_path)

                mode = WriteMode.update(current.rev)

            try:
                metadata = self.dbx.files_upload(
                    source.read(),
                    remote_path,
                    mode=mode,
                    autorename=False,
                    strict_conflict=True,
                )
            except DROPBOX_FAILURES as exc:
                raise errors.translate(exc, errors.UPLOAD, remote_path) from exc

        return DropboxFile(metadata)
    def create_folder(self, remote_path: str) -> cloud_storage_client_api.File:
        """Create a folder (and any missing parents) with ``files_create_folder_v2``.

        Raises:
        FileExistsError: If something already exists at ``remote_path``.
        ValueError: If Dropbox rejects ``remote_path`` as malformed.
        PermissionError: If Dropbox does not allow creating a folder there.
        ConnectionError: If Dropbox could not be reached or rate limited the request.
        OSError: For any other Dropbox failure, such as a full account.

        """
        try:
            res = self.dbx.files_create_folder_v2(remote_path)
        except DROPBOX_FAILURES as e:
            raise errors.translate(e, errors.CREATE_FOLDER, remote_path) from e
        return DropboxFile(res.metadata)

    def copy(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Copy a file or folder to ``to_path`` with ``files_copy_v2``.

        Dropbox copies files and folders with the same call, so one method covers both.
        Autorename is disabled, so on success the copy's path is exactly ``to_path``.

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
        except DROPBOX_FAILURES as e:
            raise errors.translate(e, errors.COPY, from_path, to_path) from e
        return DropboxFile(result.metadata)

    # ----- Read -----

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
        except DROPBOX_FAILURES as e:
            raise errors.translate(e, errors.DOWNLOAD, remote_path) from e
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
            PermissionError: If Dropbox does not allow reading metadata at ``remote_path``.
            ConnectionError: If Dropbox is unreachable or rate limited the request.
            OSError: For any other Dropbox failure.

        """
        try:
            metadata = self.dbx.files_get_metadata(remote_path)
        except DROPBOX_FAILURES as e:
            raise errors.translate(e, errors.METADATA, remote_path) from e
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
            PermissionError: If Dropbox does not allow listing ``remote_path``.
            ConnectionError: If Dropbox is unreachable or rate limited the request.
            OSError: For any other Dropbox failure.

        """
        try:
            result = self.dbx.files_list_folder(remote_path)
        except DROPBOX_FAILURES as e:
            raise errors.translate(e, errors.LIST, remote_path) from e

        for entry in result.entries:
            yield DropboxFile(entry)

        while result.has_more:
            try:
                result = self.dbx.files_list_folder_continue(result.cursor)
            except DROPBOX_FAILURES as e:
                raise errors.translate(e, errors.LIST_CONTINUE, remote_path) from e
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
            ``query`` is non-empty and at most 1000 characters, and ``max_results`` is an
            integer between 1 and 1000.

        Raises:
            ValueError: If ``query`` is empty or longer than 1000 characters, or
                ``max_results`` is not between 1 and 1000 (Dropbox is not contacted), or
                Dropbox rejects the query.
            ConnectionError: If Dropbox is unreachable or rate limited the request.
            OSError: For any other Dropbox failure.

        """
        _validate_search_arguments(query, max_results)

        options = SearchOptions(max_results=max_results, filename_only=True)
        try:
            result = self.dbx.files_search_v2(query=query, options=options)
        except DROPBOX_FAILURES as e:
            raise errors.translate(e, errors.SEARCH, query) from e
        yielded = 0

        while True:
            for match in result.matches:
                entry = _extract_search_entry(match)
                if entry is not None:
                    yield DropboxFile(entry)
                    yielded += 1
                    if yielded >= max_results:
                        return

            if not result.has_more:
                break

            try:
                result = self.dbx.files_search_continue_v2(result.cursor)
            except DROPBOX_FAILURES as e:
                raise errors.translate(e, errors.SEARCH, query) from e

    # ----- Update -----

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
        except DROPBOX_FAILURES as e:
            raise errors.translate(e, errors.MOVE, from_path, to_path) from e
        self.logger.info("Moved %s -> %s", from_path, to_path)
        return DropboxFile(result.metadata)

    # ----- Delete -----

    def delete(self, remote_path: str) -> bool:
        """Delete a file or folder (with its contents) using ``files_delete_v2``.

        Dropbox moves the entry to the account's trash, from where it can be restored in
        the Dropbox web app; this client does not expose restore.

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
        except DROPBOX_FAILURES as e:
            raise errors.translate(e, errors.DELETE, remote_path) from e
        self.logger.info("Deleted %s", remote_path)
        return True


def _validate_search_arguments(query: str, max_results: int) -> None:
    """Raise ``ValueError`` for arguments Dropbox would reject, without contacting it."""
    if not query:
        msg = "Search query must be non-empty"
        raise ValueError(msg)
    if len(query) > _SEARCH_MAX_QUERY_LENGTH:
        msg = f"Search query must be at most 1000 characters, got {len(query)}"
        raise ValueError(msg)
    if max_results <= 0 or max_results > _SEARCH_MAX_RESULTS_LIMIT:
        msg = f"max_results must be greater than 0 and at most 1000, got {max_results}"
        raise ValueError(msg)


def _reject_nested_destination(operation: str, from_path: str, to_path: str) -> None:
    """Raise ``ValueError`` if ``to_path`` is inside ``from_path``, without contacting Dropbox.

    Dropbox reports a folder moved or copied into itself with the same
    ``duplicated_or_nested_paths`` error as an operation onto the source's own path, so the
    two can only be told apart here. Paths are compared case-insensitively, as Dropbox does.
    """
    if to_path.lower().startswith(f"{from_path.lower().rstrip('/')}/"):
        msg = f"Cannot {operation} {from_path!r} into itself: {to_path!r} is inside the source"
        raise ValueError(msg)


def _extract_search_entry(
    match: files.SearchMatchV2,
) -> files.FileMetadata | files.FolderMetadata | None:
    """Return the FileMetadata or FolderMetadata from a search match, or None."""
    if match.metadata.is_metadata():
        meta = match.metadata.get_metadata()
        if isinstance(meta, files.FileMetadata | files.FolderMetadata):
            return meta
    return None


def get_client_impl() -> cloud_storage_client_api.Client:
    """Return a configured :class:`DropboxClient` instance."""
    return DropboxClient()


def register() -> None:
    """Register the Dropbox client implementation with the cloud storage client API."""
    cloud_storage_client_api.get_client = get_client_impl
