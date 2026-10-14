"""Core cloud storage client contract definitions and factory placeholder."""

from abc import ABC, abstractmethod
from collections.abc import Iterator

from cloud_storage_client_api.file import File

__all__ = ["Client", "get_client"]


class Client(ABC):
    """Abstract base class representing a cloud storage client.

    Paths are absolute within the user's storage and start with ``/``
    (for example ``/Documents/report.pdf``).
    """

    # ----- Create -----

    @abstractmethod
    def upload_file(self, local_path: str, remote_path: str, *, overwrite: bool = False) -> File:
        """Upload the file at ``local_path`` to ``remote_path`` and return its metadata."""
        raise NotImplementedError

    @abstractmethod
    def create_folder(self, remote_path: str) -> File:
        """Create a folder at ``remote_path`` and return its metadata."""
        raise NotImplementedError

    @abstractmethod
    def copy_file(self, from_path: str, to_path: str) -> File:
        """Duplicate the file at ``from_path`` to ``to_path`` and return the copy's metadata."""
        raise NotImplementedError

    @abstractmethod
    def copy_folder(self, from_path: str, to_path: str) -> File:
        """Duplicate the folder at ``from_path`` (and its contents) to ``to_path``."""
        raise NotImplementedError

    # ----- Read -----

    @abstractmethod
    def download_file(self, remote_path: str, local_path: str) -> File:
        """Download the file at ``remote_path`` to ``local_path`` and return its metadata.

        Inputs:
            remote_path: Absolute path of the remote file to download (e.g. ``/Docs/report.pdf``).
            local_path: Local filesystem path where the file contents should be written.

        Returns:
            The metadata of the downloaded file as a :class:`File`.

        Side effects:
            Writes or overwrites the local file at ``local_path``. Performs read-only operations
            on the cloud storage provider.

        Caller assumptions:
            Parent directories for ``local_path`` exist and are writable. ``remote_path`` points
            to a file, not a directory.

        Raises:
            FileNotFoundError: If nothing exists at ``remote_path``.
            IsADirectoryError: If ``remote_path`` points to a folder rather than a file.

        """
        raise NotImplementedError

    @abstractmethod
    def get_metadata(self, remote_path: str) -> File:
        """Return metadata for the file or folder at ``remote_path``.

        Inputs:
            remote_path: Absolute path of the file or folder (e.g. ``/Docs/report.pdf``).

        Returns:
            A :class:`File` instance representing the file or folder's metadata.

        Side effects:
            None. This is a read-only query on the cloud storage provider.

        Caller assumptions:
            ``remote_path`` is a non-empty absolute path starting with ``/``.

        Raises:
            FileNotFoundError: If nothing exists at ``remote_path``.

        """
        raise NotImplementedError

    @abstractmethod
    def list_folder(self, remote_path: str = "") -> Iterator[File]:
        """Return an iterator over the direct children of the folder at ``remote_path``.

        Inputs:
            remote_path: Absolute path of the folder to list (e.g. ``/Docs``), or ``""`` for root.

        Returns:
            An iterator yielding :class:`File` instances for each direct child entry in the folder.
            Pagination is handled transparently by the implementation until all entries are yielded.

        Side effects:
            None. This is a read-only query on the cloud storage provider.

        Caller assumptions:
            ``remote_path`` points to an existing folder (or is empty string for the root).

        Raises:
            FileNotFoundError: If the folder does not exist.
            ValueError: If ``remote_path`` points to a file rather than a folder.

        """
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> Iterator[File]:
        """Return an iterator over files and folders whose name matches ``query``.

        Inputs:
            query: Non-empty search string to match against entry names.
            max_results: Maximum number of entries to yield (must be > 0, defaults to 10).

        Returns:
            An iterator yielding up to ``max_results`` matching :class:`File` instances.

        Side effects:
            None. This is a read-only query on the cloud storage provider.

        Caller assumptions:
            ``query`` is non-empty and ``max_results`` is a positive integer.

        Raises:
            ValueError: If ``query`` is empty or ``max_results`` <= 0.

        """
        raise NotImplementedError

    # ----- Update -----

    @abstractmethod
    def rename(self, remote_path: str, new_name: str) -> File:
        """Rename the file or folder at ``remote_path`` in place and return its new metadata.

        The entry stays in the same parent folder; only its last path segment changes.
        Existing entries are never overwritten.

        Raises:
            ValueError: If ``new_name`` is empty or contains a ``/``.
            FileNotFoundError: If nothing exists at ``remote_path``.
            FileExistsError: If the parent folder already has an entry named ``new_name``.

        """
        raise NotImplementedError

    @abstractmethod
    def move(self, from_path: str, to_path: str) -> File:
        """Move the file or folder at ``from_path`` to ``to_path`` and return its new metadata.

        ``to_path`` is the full destination path including the entry's name
        (e.g. ``/Archive/report.pdf``). Existing entries are never overwritten.

        Raises:
            FileNotFoundError: If nothing exists at ``from_path``.
            FileExistsError: If something already exists at ``to_path``.

        """
        raise NotImplementedError

    # ----- Delete -----

    @abstractmethod
    def delete(self, remote_path: str) -> bool:
        """Delete the file or folder at ``remote_path``. Return ``True`` on success."""
        raise NotImplementedError


def get_client() -> Client:
    """Return an instance of a cloud storage Client."""
    raise NotImplementedError
