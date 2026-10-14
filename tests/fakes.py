"""In-memory ``Client`` for tests that must not touch the network or need credentials.

``FakeStorageClient`` keeps entries in a dict keyed by path and follows the documented
``Client`` contract (return values and built-in exceptions). It does not try to reproduce
Dropbox: paths are compared exactly (Dropbox compares them case-insensitively), and only
the operations some test needs are implemented. The rest raise ``NotImplementedError``;
implement one here when a test needs it, following the contract in ``client.py``.

Usage::

    storage = FakeStorageClient()
    storage.add("/Docs/report.pdf", size=2048)
    app.dependency_overrides[get_storage_client] = lambda: storage
"""

# ``tests`` is deliberately not a package (pytest runs with --import-mode=importlib), the
# same reason test_*.py files are exempt from INP001 in pyproject.toml.
# ruff: noqa: INP001

from collections.abc import Iterator
from datetime import datetime
from itertools import count
from pathlib import Path

from cloud_storage_client_api import Client, File

FIXED_MODIFIED = datetime(2026, 10, 1, 12, 30)  # noqa: DTZ001 - providers may return naive UTC


class FakeFile(File):
    """A ``File`` with fixed values; a moved or renamed entry is a new ``FakeFile``."""

    def __init__(
        self,
        file_id: str,
        path: str,
        *,
        is_folder: bool,
        size: int | None,
        modified: datetime | None,
    ) -> None:
        """Store the entry's values."""
        self._id = file_id
        self._path = path
        self._is_folder = is_folder
        self._size = size
        self._modified = modified

    @property
    def id(self) -> str:
        """Return the entry's ID; it stays the same across renames and moves."""
        return self._id

    @property
    def name(self) -> str:
        """Return the last path segment."""
        return self._path.rsplit("/", 1)[-1]

    @property
    def path(self) -> str:
        """Return the full path."""
        return self._path

    @property
    def is_folder(self) -> bool:
        """Return ``True`` for folders."""
        return self._is_folder

    @property
    def size(self) -> int | None:
        """Return the size in bytes, or ``None`` for folders."""
        return self._size

    @property
    def modified(self) -> datetime | None:
        """Return the last modified time, or ``None`` for folders."""
        return self._modified

    def moved_to(self, path: str) -> "FakeFile":
        """Return the same entry at a new path."""
        return FakeFile(
            self._id,
            path,
            is_folder=self._is_folder,
            size=self._size,
            modified=self._modified,
        )


class FakeStorageClient(Client):
    """A ``Client`` backed by a dict of path -> ``FakeFile``."""

    def __init__(self) -> None:
        """Start with no entries."""
        self.entries: dict[str, FakeFile] = {}
        self._ids = count(1)

    def add(self, path: str, *, is_folder: bool = False, size: int = 0) -> FakeFile:
        """Seed an entry at ``path`` (test setup only, not part of ``Client``)."""
        entry = FakeFile(
            f"id:{next(self._ids)}",
            path,
            is_folder=is_folder,
            size=None if is_folder else size,
            modified=None if is_folder else FIXED_MODIFIED,
        )
        self.entries[path] = entry
        return entry

    # ----- Update -----

    def rename(self, remote_path: str, new_name: str) -> File:
        """Rename in place by moving to a sibling path, as the contract describes."""
        if not new_name or "/" in new_name:
            msg = f"Invalid name {new_name!r}: must be non-empty and must not contain '/'"
            raise ValueError(msg)
        parent = remote_path.rstrip("/").rpartition("/")[0]
        return self.move(remote_path, f"{parent}/{new_name}")

    def move(self, from_path: str, to_path: str) -> File:
        """Move an entry, and a folder's contents, to ``to_path``; never overwrite."""
        if from_path not in self.entries:
            msg = f"No file or folder at {from_path!r}"
            raise FileNotFoundError(msg)
        if to_path in self.entries:
            msg = f"Something already exists at {to_path!r}"
            raise FileExistsError(msg)
        for path in [p for p in self.entries if p == from_path or p.startswith(f"{from_path}/")]:
            new_path = to_path + path.removeprefix(from_path)
            self.entries[new_path] = self.entries.pop(path).moved_to(new_path)
        return self.entries[to_path]

    # ----- Not implemented until a test needs them -----

    def upload_file(self, local_path: str, remote_path: str, *, overwrite: bool = False) -> File:
        """Upload a local file to ``remote_path``; overwrite files only when requested."""
        with Path(local_path).open("rb") as source:
            source.seek(0, 2)
            size = source.tell()

        existing = self.entries.get(remote_path)
        if existing is not None and (existing.is_folder or not overwrite):
            msg = f"Something already exists at {remote_path!r}"
            raise FileExistsError(msg)

        file_id = existing.id if existing is not None else f"id:{next(self._ids)}"
        uploaded = FakeFile(
            file_id,
            remote_path,
            is_folder=False,
            size=size,
            modified=FIXED_MODIFIED,
        )
        self.entries[remote_path] = uploaded
        return uploaded

    def create_folder(self, remote_path: str) -> File:
        """Create an empty folder at ``remote_path``; never overwrite an entry."""
        if remote_path in self.entries:
            msg = f"Something already exists at {remote_path!r}"
            raise FileExistsError(msg)
        return self.add(remote_path, is_folder=True)

    def copy_file(self, from_path: str, to_path: str) -> File:
        """Copy the entry at ``from_path`` to ``to_path``."""
        return self._copy_entry(from_path, to_path)

    def copy_folder(self, from_path: str, to_path: str) -> File:
        """Copy the entry at ``from_path`` and any descendants to ``to_path``."""
        return self._copy_entry(from_path, to_path)

    def _copy_entry(self, from_path: str, to_path: str) -> FakeFile:
        """Copy a file or folder tree to a new path without changing the source."""
        if from_path not in self.entries:
            msg = f"No file or folder at {from_path!r}"
            raise FileNotFoundError(msg)
        if to_path in self.entries:
            msg = f"Something already exists at {to_path!r}"
            raise FileExistsError(msg)

        source_paths = [path for path in self.entries if path == from_path or path.startswith(f"{from_path}/")]
        copies: list[FakeFile] = []
        for source_path in source_paths:
            source = self.entries[source_path]
            destination_path = to_path + source_path.removeprefix(from_path)
            copies.append(
                FakeFile(
                    f"id:{next(self._ids)}",
                    destination_path,
                    is_folder=source.is_folder,
                    size=source.size,
                    modified=source.modified,
                ),
            )

        for copied in copies:
            self.entries[copied.path] = copied
        return self.entries[to_path]

    def download_file(self, remote_path: str, local_path: str) -> File:
        """Not implemented yet."""
        raise NotImplementedError

    def get_metadata(self, remote_path: str) -> File:
        """Not implemented yet."""
        raise NotImplementedError

    def list_folder(self, remote_path: str = "") -> Iterator[File]:
        """Not implemented yet."""
        raise NotImplementedError

    def search(self, query: str, max_results: int = 10) -> Iterator[File]:
        """Not implemented yet."""
        raise NotImplementedError

    def delete(self, remote_path: str) -> bool:
        """Not implemented yet."""
        raise NotImplementedError
