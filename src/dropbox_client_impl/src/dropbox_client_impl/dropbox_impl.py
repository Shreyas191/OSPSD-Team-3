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

from dropbox_client_impl.auth import get_dropbox_client


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
        """Rename in place. TODO(Shreyas): implement with ``files_move_v2``."""
        raise NotImplementedError

    def move(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Move a file or folder. TODO(Shreyas): implement with ``files_move_v2``."""
        raise NotImplementedError

    # ----- Delete (John) -----

    def delete(self, remote_path: str) -> bool:
        """Delete a file or folder. TODO(John): implement with ``files_delete_v2``."""
        raise NotImplementedError


def get_client_impl() -> cloud_storage_client_api.Client:
    """Return a configured :class:`DropboxClient` instance."""
    return DropboxClient()


def register() -> None:
    """Register the Dropbox client implementation with the cloud storage client API."""
    cloud_storage_client_api.get_client = get_client_impl
