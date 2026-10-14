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
from dropbox import files
from dropbox.files import WriteMode

from dropbox_client_impl.auth import get_dropbox_client
from dropbox_client_impl.file_impl import DropboxFile


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
    """"
    Level 1.3: Specification
    Operation: Create a new file with the contents provided in the request
    HTTP Method: POST
    route/endpoint: /2/files/upload

    Request Header Parameters: (No body parameters are required for this operation)
        - REQUIRED: Dropbox-API-Arg: A JSON object containing the following details:
            - path (string): The path in the user's Dropbox where the file will be created. This
            should include the file name and extension.
            - auto_rename (boolean, optional): If true, the file will be automatically renamed if
            a file with the same name already exists.
            Default is false.
            - client_modified (datetime, optional): The timestamp when the file was last modified
            on the client side. If not provided, the
            current time will be used.
            - content_hash (string, optional): A hash of the file content. If provided, Dropbox
            will verify that the uploaded file matches this hash.
            - mode (string, optional): The file mode to use when creating the file. Possible
            values are "add" (default), "overwrite", and "update".
            - mute (boolean, optional): If true, the file will be created without sending a
            notification to the user. Default is false.

    Success Response Header:
        - 200 OK: The file was successfully created.
        - X-Dropbox-Request-Id: A unique identifier for the request, which can be used for
        troubleshooting and support.

    Success Response Body: A JSON object containing the following details:
        - name (string): The name of the newly created file.
        - id (string): A unique identifier for the file.
        - rev (string): A revision identifier for the file, which can be used to track changes.
        - server_modified (datetime): The timestamp when the file was last modified on the server
        side.
        - client_modified (datetime): The timestamp when the file was last modified on the client
        side.
        - size (integer): The size of the file in bytes.
        - content_hash (string): A hash of the file content, which can be used to verify the
        integrity of the uploaded file.
        - export_info (object, optional): Information about the file's export settings, if
        applicable.
        - file_lock_info (object, optional): Information about the file's lock status, if
        applicable.
        - has_explicit_shared_members (boolean): Indicates whether the file has any explicitly
        shared members.
        - is_downloadable (boolean): Indicates whether the file can be downloaded.
        - is_restorable (boolean): Indicates whether the file can be restored from a previous
        version.
        - path_lower (string): The lowercase path of the file in the user's Dropbox.
        - preview_url (string, optional): A URL that can be used to preview the file
    """

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = False,
    ) -> cloud_storage_client_api.File:
        """Upload a local file to Dropbox. TODO(Zesan): implement with ``files_upload``."""
        mode = WriteMode.overwrite if overwrite else WriteMode.add

        with Path(local_path).open("rb") as source:
            metadata = self.dbx.files_upload(
                source.read(),
                remote_path,
                mode=mode,
                autorename=False,  # Will return error on duplicate file name
            )

        return DropboxFile(metadata)

    def create_folder(self, remote_path: str) -> cloud_storage_client_api.File:
        """Create a folder."""
        res = self.dbx.files_create_folder_v2(remote_path)
        return DropboxFile(res.metadata)

    def copy_file(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Duplicate a file."""
        res = self.dbx.files_copy_v2(from_path, to_path)
        return DropboxFile(res.metadata)

    def copy_folder(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Duplicate a folder."""
        res = self.dbx.files_copy_v2(from_path, to_path)
        return DropboxFile(res.metadata)

    # ----- Read (Jing) -----

    def download_file(self, remote_path: str, local_path: str) -> cloud_storage_client_api.File:
        """Download a file. TODO(Jing): implement with ``files_download_to_file``."""
        raise NotImplementedError

    def get_metadata(self, remote_path: str) -> cloud_storage_client_api.File:
        """Get file or folder metadata. TODO(Jing): implement with ``files_get_metadata``."""
        raise NotImplementedError

    def list_folder(self, remote_path: str = "") -> Iterator[cloud_storage_client_api.File]:
        """List a folder. TODO(Jing): implement with ``files_list_folder`` (+ ``_continue``)."""
        raise NotImplementedError

    def search(self, query: str, max_results: int = 10) -> Iterator[cloud_storage_client_api.File]:
        """Search by name. TODO(Jing): implement with ``files_search_v2``."""
        raise NotImplementedError

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
