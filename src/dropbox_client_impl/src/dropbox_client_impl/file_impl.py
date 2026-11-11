"""Dropbox File Implementation.

This module adapts the metadata objects returned by the Dropbox SDK
(``FileMetadata`` and ``FolderMetadata``) to the ``cloud_storage_client_api.File`` contract.
"""

from datetime import UTC, datetime

import cloud_storage_client_api
from dropbox import files


class DropboxFile(cloud_storage_client_api.File):
    """Concrete implementation of the File abstraction backed by Dropbox metadata."""

    def __init__(self, metadata: files.Metadata) -> None:
        """Wrap a Dropbox ``FileMetadata`` or ``FolderMetadata`` object.

        Raises:
            TypeError: For any other metadata, such as ``DeletedMetadata``, which has no ID
                and does not describe an existing entry. Receiving one is a bug in the
                calling ``DropboxClient`` method, not a caller error.

        """
        if not isinstance(metadata, files.FileMetadata | files.FolderMetadata):
            msg = f"Expected FileMetadata or FolderMetadata, got {type(metadata).__name__}"
            raise TypeError(msg)
        self._metadata = metadata

    @property
    def id(self) -> str:
        """Return the Dropbox ID of the file or folder (e.g. ``id:abc123``)."""
        return str(self._metadata.id)

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
        """Return the server-side last-modified time in UTC, or ``None`` for folders.

        The SDK returns Dropbox's UTC timestamp without a time zone; it is marked as UTC
        here so callers never have to guess.
        """
        if isinstance(self._metadata, files.FileMetadata):
            modified: datetime = self._metadata.server_modified
            return modified.replace(tzinfo=UTC) if modified.tzinfo is None else modified
        return None

    @property
    def etag(self) -> str | None:
        """Return the file's Dropbox revision (``rev``), or ``None`` for folders.

        Dropbox gives a file a new ``rev`` every time its contents change.
        """
        if isinstance(self._metadata, files.FileMetadata):
            return str(self._metadata.rev)
        return None

    def __repr__(self) -> str:
        """Return a readable representation for debugging."""
        kind = "folder" if self.is_folder else "file"
        return f"DropboxFile({kind}, path={self.path!r})"
