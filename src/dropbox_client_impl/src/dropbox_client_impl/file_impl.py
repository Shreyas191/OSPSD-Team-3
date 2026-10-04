"""Dropbox File Implementation.

This module adapts the metadata objects returned by the Dropbox SDK
(``FileMetadata`` and ``FolderMetadata``) to the ``cloud_storage_client_api.File`` contract.
"""

from datetime import datetime

import cloud_storage_client_api
from dropbox import files


class DropboxFile(cloud_storage_client_api.File):
    """Concrete implementation of the File abstraction backed by Dropbox metadata."""

    def __init__(self, metadata: files.Metadata) -> None:
        """Wrap a Dropbox ``FileMetadata`` or ``FolderMetadata`` object."""
        self._metadata = metadata

    @property
    def id(self) -> str:
        """Return the Dropbox ID of the file or folder (e.g. ``id:abc123``)."""
        return str(getattr(self._metadata, "id", ""))

    @property
    def name(self) -> str:
        """Return the name of the file or folder."""
        return str(self._metadata.name)

    @property
    def path(self) -> str:
        """Return the display path of the file or folder."""
        return str(self._metadata.path_display or "")

    @property
    def is_folder(self) -> bool:
        """Return ``True`` if the metadata describes a folder."""
        return isinstance(self._metadata, files.FolderMetadata)

    @property
    def size(self) -> int | None:
        """Return the file size in bytes, or ``None`` for folders."""
        if isinstance(self._metadata, files.FileMetadata):
            return int(self._metadata.size)
        return None

    @property
    def modified(self) -> datetime | None:
        """Return the server-side last-modified time, or ``None`` for folders."""
        if isinstance(self._metadata, files.FileMetadata):
            modified: datetime = self._metadata.server_modified
            return modified
        return None

    def __repr__(self) -> str:
        """Return a readable representation for debugging."""
        kind = "folder" if self.is_folder else "file"
        return f"DropboxFile({kind}, path={self.path!r})"
