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

        Inputs:
            local_path: Path of the local file to upload.
            remote_path: Absolute destination path including the file name
                (e.g. ``/Docs/report.pdf``).
            overwrite: Replace an existing file at ``remote_path``. Defaults to false.

        Returns:
            The uploaded file. A file that replaced an existing one keeps that file's ``id``.

        Side effects:
            Creates or replaces the file in the provider. Missing parent folders are
            created too. A folder at ``remote_path`` is never replaced.

        Raises:
            FileNotFoundError: If ``local_path`` does not exist (checked before contacting
                the provider).
            FileExistsError: If something already exists at ``remote_path`` and
                ``overwrite`` is false, or a folder is there.
            ValueError: If the provider rejects ``remote_path`` as malformed or the file is
                too large for it to accept in one upload.
            PermissionError: If the provider does not allow writing at ``remote_path``.
            ConnectionError: If the provider could not be reached or is temporarily too
                busy; retrying later may succeed.
            OSError: For any other provider failure, such as a full account.

        """
        raise NotImplementedError

    @abstractmethod
    def create_folder(self, remote_path: str) -> File:
        """Create a folder at ``remote_path`` and return its metadata.

        Inputs:
            remote_path: Absolute path of the new folder (e.g. ``/Docs/New``).

        Returns:
            The new folder (its ``size`` and ``modified`` are ``None``).

        Side effects:
            Creates the folder in the provider. Missing parent folders are created too.

        Raises:
            FileExistsError: If something already exists at ``remote_path``.
            ValueError: If the provider rejects ``remote_path`` as malformed.
            PermissionError: If the provider does not allow creating a folder there.
            ConnectionError: If the provider could not be reached or is temporarily too
                busy; retrying later may succeed.
            OSError: For any other provider failure, such as a full account.

        """
        raise NotImplementedError

    @abstractmethod
    def copy(self, from_path: str, to_path: str) -> File:
        """Copy the file or folder at ``from_path`` to ``to_path`` and return the copy's metadata.

        Inputs:
            from_path: Absolute path of the file or folder to copy.
            to_path: Full destination path including the copy's name
                (e.g. ``/Archive/report.pdf``).

        Returns:
            The copy at ``to_path``. It is a new entry with its own ``id``.

        Side effects:
            Creates the copy in the provider; a folder is copied with its contents. The
            source is unchanged, and existing entries are never overwritten.

        Raises:
            ValueError: If ``to_path`` is inside ``from_path`` (a folder copied into
                itself), or the provider rejects a path as malformed.
            FileNotFoundError: If nothing exists at ``from_path``.
            FileExistsError: If something already exists at ``to_path``, including the
                source itself.
            PermissionError: If the provider does not allow the copy.
            ConnectionError: If the provider could not be reached or is temporarily too
                busy; retrying later may succeed.
            OSError: For any other provider failure, such as a full account.

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

        Inputs:
            remote_path: Absolute path of the entry to rename (e.g. ``/Docs/report.pdf``).
            new_name: The entry's new name, without any ``/`` (e.g. ``summary.pdf``).

        Returns:
            The entry at its new path. It is the same entry, so its ``id`` is unchanged.

        Side effects:
            Changes the entry's name in the provider. The entry stays in the same parent
            folder; only its last path segment changes. Existing entries are never
            overwritten.

        Raises:
            ValueError: If ``new_name`` is empty or contains a ``/`` (checked before
                contacting the provider), or the provider rejects the path.
            FileNotFoundError: If nothing exists at ``remote_path``.
            FileExistsError: If the parent folder already has an entry named ``new_name``,
                including the entry itself (renaming to the current name).
            PermissionError, ConnectionError, OSError: As for :meth:`move`.

        """
        raise NotImplementedError

    @abstractmethod
    def move(self, from_path: str, to_path: str) -> File:
        """Move the file or folder at ``from_path`` to ``to_path`` and return its new metadata.

        Inputs:
            from_path: Absolute path of the entry to move (e.g. ``/Docs/report.pdf``).
            to_path: Full destination path including the entry's name
                (e.g. ``/Archive/report.pdf``); the name may change too.

        Returns:
            The entry at ``to_path``. It is the same entry, so its ``id`` is unchanged.

        Side effects:
            Moves the entry, and everything inside it if it is a folder, in the provider.
            Existing entries are never overwritten. Moving the same entry again raises
            ``FileNotFoundError``, since nothing is left at ``from_path``.

        Raises:
            ValueError: If the provider rejects a path as malformed, or ``to_path`` is
                inside ``from_path`` (a folder moved into itself).
            FileNotFoundError: If nothing exists at ``from_path``.
            FileExistsError: If something already exists at ``to_path``, including the
                entry itself.
            PermissionError: If the provider does not allow the move (for example, the
                entry is locked or the destination is read-only for this account).
            ConnectionError: If the provider could not be reached or is temporarily too
                busy. A rate-limited move did not happen. After a lost connection it may
                or may not have happened; retrying is safe, because a repeat after success
                raises ``FileNotFoundError`` rather than moving anything twice.
            OSError: For any other provider failure, such as a full account.

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
            ValueError: If ``remote_path`` is empty or the root (``/``), which is checked
                before contacting the provider, or if the provider rejects the path as
                malformed.
            FileNotFoundError: If nothing exists at ``remote_path``.
            PermissionError: If the provider does not allow deleting the entry (for
                example, it is locked or read-only for this account).
            ConnectionError: If the provider could not be reached or is temporarily too
                busy; nothing was deleted and retrying later may succeed.
            OSError: If the provider failed for any other reason. The exceptions above
                except ``ValueError`` are ``OSError`` subclasses, so catch them first.

        """
        raise NotImplementedError


def get_client() -> Client:
    """Return an instance of a cloud storage Client."""
    raise NotImplementedError
