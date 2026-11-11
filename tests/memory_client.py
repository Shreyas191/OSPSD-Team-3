"""An in-memory ``Client`` that behaves like Dropbox where the shared contract can tell.

``scripts/serve_in_memory.py`` serves the real HTTP app on top of it, so CI can run the
vertical's conformance suite on every pull request without network or credentials. Unlike
``tests/fakes.py`` (a minimal fake for route unit tests), it models what the suite checks:

- file contents are stored, so a download returns the bytes that were uploaded;
- writing ``/a/b/c.txt`` creates the folders ``/a`` and ``/a/b``, as Dropbox does;
- paths are compared ignoring case, as Dropbox does, and keep the case they were created with;
- every write sets ``modified`` to the current UTC time and a new ``etag``.

It raises the exceptions each ``Client`` method documents. It is not a model of Dropbox's
limits or failures: nothing is too large, and nothing is ever unavailable.
"""

# ``tests`` is deliberately not a package (see tests/fakes.py).
# ruff: noqa: INP001

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

from cloud_storage_client_api import Client, File


@dataclass(frozen=True)
class _Entry:
    id: str
    path: str
    data: bytes | None  # None for folders
    modified: datetime | None
    etag: str | None


class MemoryFile(File):
    """A snapshot of one entry, as a ``File``."""

    def __init__(self, entry: _Entry) -> None:
        """Wrap an entry."""
        self._entry = entry

    @property
    def id(self) -> str:
        """Return the entry's ID; it survives moves and replaces."""
        return self._entry.id

    @property
    def name(self) -> str:
        """Return the last path segment."""
        return self._entry.path.rsplit("/", 1)[-1]

    @property
    def path(self) -> str:
        """Return the full path, in the case it was created with."""
        return self._entry.path

    @property
    def is_folder(self) -> bool:
        """Return ``True`` for folders."""
        return self._entry.data is None

    @property
    def size(self) -> int | None:
        """Return the size in bytes, or ``None`` for folders."""
        return None if self._entry.data is None else len(self._entry.data)

    @property
    def modified(self) -> datetime | None:
        """Return the last write time in UTC, or ``None`` for folders."""
        return self._entry.modified

    @property
    def etag(self) -> str | None:
        """Return the version fingerprint, or ``None`` for folders."""
        return self._entry.etag


def _key(path: str) -> str:
    return path.rstrip("/").lower()


def _parent(path: str) -> str:
    return path.rstrip("/").rpartition("/")[0]


class InMemoryClient(Client):
    """A ``Client`` backed by a dict of lower-cased path -> entry."""

    def __init__(self) -> None:
        """Start with an empty storage."""
        self._entries: dict[str, _Entry] = {}
        self._ids = count(1)
        self._versions = count(1)

    # ----- Helpers -----

    def _get(self, path: str) -> _Entry:
        entry = self._entries.get(_key(path))
        if entry is None:
            msg = f"No file or folder at {path!r}"
            raise FileNotFoundError(msg)
        return entry

    def _is_folder(self, path: str) -> bool:
        return path in {"", "/"} or (_key(path) in self._entries and self._entries[_key(path)].data is None)

    def _make_parents(self, path: str) -> None:
        parent = _parent(path)
        if not parent:
            return
        existing = self._entries.get(_key(parent))
        if existing is not None and existing.data is not None:
            msg = f"Cannot write {path!r}: {existing.path!r} is a file"
            raise ValueError(msg)
        if existing is None:
            self._make_parents(parent)
            self._entries[_key(parent)] = _Entry(f"id:{next(self._ids)}", parent, None, None, None)

    def _write(self, path: str, data: bytes, entry_id: str) -> MemoryFile:
        entry = _Entry(entry_id, path, data, datetime.now(UTC), f"rev{next(self._versions):08x}")
        self._entries[_key(path)] = entry
        return MemoryFile(entry)

    def _subtree(self, path: str) -> list[str]:
        prefix = _key(path) + "/"
        return [k for k in self._entries if k == _key(path) or k.startswith(prefix)]

    # ----- Create -----

    def upload_file(self, local_path: str, remote_path: str, *, overwrite: bool = False) -> File:
        """Store the file's bytes at ``remote_path``; overwrite a file only when asked."""
        data = Path(local_path).read_bytes()
        existing = self._entries.get(_key(remote_path))
        if existing is not None and (existing.data is None or not overwrite):
            msg = f"Something already exists at {remote_path!r}"
            raise FileExistsError(msg)
        self._make_parents(remote_path)
        entry_id = existing.id if existing is not None else f"id:{next(self._ids)}"
        return self._write(remote_path, data, entry_id)

    def replace_file(self, local_path: str, remote_path: str) -> File:
        """Replace an existing file's bytes; never create one."""
        existing = self._get(remote_path)
        if existing.data is None:
            msg = f"Path is a folder: {remote_path!r}"
            raise IsADirectoryError(msg)
        return self._write(existing.path, Path(local_path).read_bytes(), existing.id)

    def create_folder(self, remote_path: str) -> File:
        """Create a folder and any missing parents."""
        if _key(remote_path) in self._entries:
            msg = f"Something already exists at {remote_path!r}"
            raise FileExistsError(msg)
        self._make_parents(remote_path)
        entry = _Entry(f"id:{next(self._ids)}", remote_path, None, None, None)
        self._entries[_key(remote_path)] = entry
        return MemoryFile(entry)

    def copy(self, from_path: str, to_path: str) -> File:
        """Copy a file, or a folder with its contents; every copy gets a new ID."""
        if _key(to_path).startswith(_key(from_path) + "/"):
            msg = f"Cannot copy {from_path!r} into itself"
            raise ValueError(msg)
        source = self._get(from_path)
        if _key(to_path) in self._entries:
            msg = f"Something already exists at {to_path!r}"
            raise FileExistsError(msg)
        self._make_parents(to_path)
        for key in self._subtree(source.path):
            old = self._entries[key]
            new_path = to_path + old.path[len(source.path) :]
            self._entries[_key(new_path)] = replace(old, id=f"id:{next(self._ids)}", path=new_path)
        return MemoryFile(self._entries[_key(to_path)])

    # ----- Read -----

    def download_file(self, remote_path: str, local_path: str) -> File:
        """Write the stored bytes to ``local_path``."""
        entry = self._get(remote_path)
        if entry.data is None:
            msg = f"Path is a folder: {remote_path!r}"
            raise IsADirectoryError(msg)
        Path(local_path).write_bytes(entry.data)
        return MemoryFile(entry)

    def get_metadata(self, remote_path: str) -> File:
        """Return the entry at ``remote_path``."""
        return MemoryFile(self._get(remote_path))

    def list_folder(self, remote_path: str = "") -> Iterator[File]:
        """Yield the direct children of a folder (``""`` is the root)."""
        if remote_path not in {"", "/"}:
            folder = self._get(remote_path)
            if folder.data is not None:
                msg = f"Path is a file: {remote_path!r}"
                raise ValueError(msg)
        parent = _key(remote_path)
        for entry in list(self._entries.values()):
            if _key(_parent(entry.path)) == parent:
                yield MemoryFile(entry)

    def search(self, query: str, max_results: int = 10) -> Iterator[File]:
        """Yield up to ``max_results`` entries whose name contains ``query``."""
        if not query:
            msg = "Search query must be non-empty"
            raise ValueError(msg)
        matches = (e for e in self._entries.values() if query.lower() in e.path.rsplit("/", 1)[-1].lower())
        for found, entry in enumerate(matches):
            if found >= max_results:
                return
            yield MemoryFile(entry)

    # ----- Update -----

    def rename(self, remote_path: str, new_name: str) -> File:
        """Rename in place by moving to a sibling path."""
        if not new_name or "/" in new_name:
            msg = f"Invalid name {new_name!r}"
            raise ValueError(msg)
        return self.move(remote_path, f"{_parent(remote_path)}/{new_name}")

    def move(self, from_path: str, to_path: str) -> File:
        """Move an entry and its contents; never overwrite; keep IDs."""
        if _key(to_path).startswith(_key(from_path) + "/"):
            msg = f"Cannot move {from_path!r} into itself"
            raise ValueError(msg)
        source = self._get(from_path)
        if _key(to_path) in self._entries:
            msg = f"Something already exists at {to_path!r}"
            raise FileExistsError(msg)
        self._make_parents(to_path)
        for key in self._subtree(source.path):
            old = self._entries.pop(key)
            new_path = to_path + old.path[len(source.path) :]
            modified = datetime.now(UTC) if old.data is not None else None
            self._entries[_key(new_path)] = replace(old, path=new_path, modified=modified)
        return MemoryFile(self._entries[_key(to_path)])

    # ----- Health -----

    def check_health(self) -> None:
        """Report healthy: there is no provider to reach."""

    # ----- Delete -----

    def delete(self, remote_path: str) -> bool:
        """Delete an entry and a folder's contents; never the root."""
        if not remote_path.strip("/"):
            msg = f"Refusing to delete the root folder (path {remote_path!r})"
            raise ValueError(msg)
        entry = self._get(remote_path)
        for key in self._subtree(entry.path):
            del self._entries[key]
        return True
