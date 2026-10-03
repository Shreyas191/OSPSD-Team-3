"""Tests for the File abstract base class contract."""

from datetime import UTC, datetime

import pytest

from cloud_storage_client_api import File

SIZE = 2048


class _InMemoryFile(File):
    """Minimal concrete File used to exercise the contract."""

    def __init__(self, path: str, size: int | None, modified: datetime | None) -> None:
        self._path = path
        self._size = size
        self._modified = modified

    @property
    def id(self) -> str:
        return f"id:{self._path}"

    @property
    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_folder(self) -> bool:
        return self._size is None

    @property
    def size(self) -> int | None:
        return self._size

    @property
    def modified(self) -> datetime | None:
        return self._modified


def test_file_cannot_be_instantiated() -> None:
    """File is abstract and cannot be created directly."""
    with pytest.raises(TypeError):
        File()  # type: ignore[abstract]


def test_file_declares_expected_properties() -> None:
    """Every implementation must provide these properties."""
    assert {"id", "name", "path", "is_folder", "size", "modified"} == File.__abstractmethods__


def test_concrete_file_satisfies_contract() -> None:
    """A complete subclass can be instantiated and read."""
    modified = datetime(2026, 10, 1, tzinfo=UTC)
    report = _InMemoryFile("/Docs/report.pdf", SIZE, modified)

    assert report.id == "id:/Docs/report.pdf"
    assert report.name == "report.pdf"
    assert report.is_folder is False
    assert report.size == SIZE
    assert report.modified == modified


def test_folder_has_no_size_or_modified() -> None:
    """Folders report ``None`` for size and modified time."""
    folder = _InMemoryFile("/Docs", None, None)

    assert folder.is_folder is True
    assert folder.size is None
    assert folder.modified is None
