"""Dropbox Client Implementation.

This module provides a concrete implementation of the cloud storage client API using
the official Dropbox Python SDK.

Authentication uses a long-lived OAuth2 refresh token read from environment variables
(or a local ``.env`` file), so it works the same way locally and in CI/CD.

Each method below is a stub owned by a team member. Replace the ``NotImplementedError``
with a real Dropbox call on your own ``<name>-<feature>`` branch.
"""

import logging
import os
from collections.abc import Iterator
from typing import ClassVar

import cloud_storage_client_api
import dropbox
from dotenv import load_dotenv
from dropbox import files

from dropbox_client_impl.file_impl import DropboxFile

load_dotenv()


class DropboxClient(cloud_storage_client_api.Client):
    """Concrete implementation of the Client abstraction using the Dropbox API.

    Environment Variables:
        - DROPBOX_APP_KEY: App key from the Dropbox App Console
        - DROPBOX_APP_SECRET: App secret from the Dropbox App Console
        - DROPBOX_REFRESH_TOKEN: Long-lived OAuth2 refresh token for the account

    """

    REQUIRED_ENV_VARS: ClassVar[tuple[str, ...]] = (
        "DROPBOX_APP_KEY",
        "DROPBOX_APP_SECRET",
        "DROPBOX_REFRESH_TOKEN",
    )

    def __init__(self, dbx: dropbox.Dropbox | None = None) -> None:
        """Initialize the DropboxClient.

        Args:
            dbx: An already-configured ``dropbox.Dropbox`` instance. When provided,
                authentication is skipped (useful for tests).

        Raises:
            RuntimeError: If no instance is provided and credentials are missing.

        """
        self.logger = logging.getLogger(__name__)
        if dbx is not None:
            self.dbx = dbx
            return

        missing = [name for name in self.REQUIRED_ENV_VARS if not os.environ.get(name)]
        if missing:
            msg = f"No valid credentials found. Missing environment variables: {', '.join(missing)}"
            raise RuntimeError(msg)

        self.dbx = dropbox.Dropbox(
            oauth2_refresh_token=os.environ["DROPBOX_REFRESH_TOKEN"],
            app_key=os.environ["DROPBOX_APP_KEY"],
            app_secret=os.environ["DROPBOX_APP_SECRET"],
        )

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
        the returned file's path is exactly ``to_path`` (modulo case).

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
    if reason.is_to() and reason.get_to().is_conflict():
        return FileExistsError(f"Something already exists at {to_path!r}")
    return None


def get_client_impl() -> cloud_storage_client_api.Client:
    """Return a configured :class:`DropboxClient` instance."""
    return DropboxClient()


def register() -> None:
    """Register the Dropbox client implementation with the cloud storage client API."""
    cloud_storage_client_api.get_client = get_client_impl
