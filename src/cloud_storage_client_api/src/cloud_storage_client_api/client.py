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
        """Upload the file at ``local_path`` to ``remote_path`` and return its metadata.

        An existing file at ``remote_path`` is replaced only when ``overwrite`` is true.

        Raises:
            FileNotFoundError: If ``local_path`` does not exist.
            FileExistsError: If something already exists at ``remote_path`` and
                ``overwrite`` is false.

        """
        raise NotImplementedError

    @abstractmethod
    def create_folder(self, remote_path: str) -> File:
        """Create a folder at ``remote_path`` and return its metadata.

        Missing parent folders are created too.

        Raises:
            FileExistsError: If something already exists at ``remote_path``.

        """
        raise NotImplementedError

    @abstractmethod
    def copy(self, from_path: str, to_path: str) -> File:
        """Copy the file or folder at ``from_path`` to ``to_path`` and return the copy's metadata.

        ``to_path`` is the full destination path including the entry's name. A folder is
        copied with its contents. The source is unchanged, the copy gets its own ID, and
        existing entries are never overwritten.

        Raises:
            FileNotFoundError: If nothing exists at ``from_path``.
            FileExistsError: If something already exists at ``to_path``.

        """
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
            max_results: Maximum number of entries to yield (must be between 1 and 1000,
                defaults to 10).

        Returns:
            An iterator yielding up to ``max_results`` matching :class:`File` instances.

        Side effects:
            None. This is a read-only query on the cloud storage provider.

        Caller assumptions:
            ``query`` is non-empty and ``max_results`` is an integer between 1 and 1000.

        Raises:
            ValueError: If ``query`` is empty or ``max_results`` is not between 1 and 1000.

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
        """Delete the file or folder at ``remote_path``, including a folder's contents.

        Inputs:
            remote_path: Absolute path of the file or folder to delete (e.g. ``/Docs/report.pdf``).

        Returns:
            ``True`` once the entry has been deleted. Failures raise instead of returning
            ``False``.

        Side effects:
            Removes the entry, and everything inside it if it is a folder, from the cloud
            storage provider. Deleting the same path again raises ``FileNotFoundError``.

        Caller assumptions:
            ``remote_path`` is an absolute path starting with ``/`` that names an entry
            below the root; the root itself can never be deleted.

        Raises:
            ValueError: If ``remote_path`` is empty or the root (``/``). This is checked
                before contacting the provider.
            FileNotFoundError: If nothing exists at ``remote_path``.

        """
        raise NotImplementedError


def get_client() -> Client:
    """Return an instance of a cloud storage Client."""
    raise NotImplementedError
