"""File contract - Core representation of a file or folder in cloud storage."""

from abc import ABC, abstractmethod
from datetime import datetime


class File(ABC):
    """Abstract base class representing a file or folder stored in the cloud."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Return the provider's unique identifier for the file or folder."""
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the name of the file or folder, including any extension."""
        raise NotImplementedError

    @property
    @abstractmethod
    def path(self) -> str:
        """Return the full path of the file or folder, e.g. ``/Documents/report.pdf``."""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_folder(self) -> bool:
        """Return ``True`` if this entry is a folder, ``False`` if it is a file."""
        raise NotImplementedError

    @property
    @abstractmethod
    def size(self) -> int | None:
        """Return the size in bytes, or ``None`` for folders."""
        raise NotImplementedError

    @property
    @abstractmethod
    def modified(self) -> datetime | None:
        """Return when the file was last modified, or ``None`` for folders."""
        raise NotImplementedError
