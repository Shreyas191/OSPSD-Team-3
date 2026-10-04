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
        """Download the file at ``remote_path`` to ``local_path`` and return its metadata."""
        raise NotImplementedError

    @abstractmethod
    def get_metadata(self, remote_path: str) -> File:
        """Return the metadata (name, size, modified date, ...) for ``remote_path``."""
        raise NotImplementedError

    @abstractmethod
    def list_folder(self, remote_path: str = "") -> Iterator[File]:
        """Return an iterator over the entries in a folder (``""`` is the root)."""
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> Iterator[File]:
        """Return an iterator over files and folders whose name matches ``query``."""
        raise NotImplementedError

    # ----- Update -----

    @abstractmethod
    def rename(self, remote_path: str, new_name: str) -> File:
        """Rename the file or folder at ``remote_path`` in place and return its new metadata."""
        raise NotImplementedError

    @abstractmethod
    def move(self, from_path: str, to_path: str) -> File:
        """Move the file or folder at ``from_path`` to ``to_path``."""
        raise NotImplementedError

    # ----- Delete -----

    @abstractmethod
    def delete(self, remote_path: str) -> bool:
        """Delete the file or folder at ``remote_path``. Return ``True`` on success."""
        raise NotImplementedError


def get_client() -> Client:
    """Return an instance of a cloud storage Client."""
    raise NotImplementedError
