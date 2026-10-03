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

    def copy(self, from_path: str, to_path: str) -> cloud_storage_client_api.File:
        """Duplicate a file or folder. TODO(Zesan): implement with ``files_copy_v2``."""
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
