"""Our Dropbox implementation of the shared Document Storage contract (``cloud_storage_api``).

The class shares one contract package per vertical (``Dayana20/ospsd26-cloud-storage`` for
storage). :class:`DropboxDocumentStorage` implements its ``DocumentStorage`` interface, so a
consumer that codes against the shared contract can use our Dropbox implementation, or
another team's, by configuration alone.

It is a thin layer over a ``cloud_storage_client_api.Client`` (``DropboxClient`` by
default), which already talks to Dropbox and translates every SDK failure into a built-in
exception. This layer adds what the shared contract needs on top:

- bytes in and out instead of local file paths;
- the shared ``FileInfo`` model instead of ``File``;
- the shared error classes instead of built-in exceptions (see :func:`_shared_errors`);
- the contract's rules for paths and moves, checked before Dropbox is contacted.
"""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import cloud_storage_client_api
from cloud_storage_api import (
    ConflictError,
    DocumentStorage,
    FileInfo,
    FileTooLargeError,
    InvalidOperationError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    ProviderUnavailableError,
)

from dropbox_client_impl.dropbox_impl import DropboxClient

_ROOT = "/"


class DropboxDocumentStorage(DocumentStorage):
    """The shared ``DocumentStorage`` contract, backed by Dropbox.

    Usage::

        storage = DropboxDocumentStorage()          # Dropbox credentials from the environment
        info = storage.create("/notes/standup.md", b"# Standup")
        storage.download(info.path)                 # b"# Standup"

    Every method raises only the shared ``cloud_storage_api`` errors.
    """

    def __init__(self, client: cloud_storage_client_api.Client | None = None) -> None:
        """Wrap ``client``, or a ``DropboxClient`` built from the environment.

        Raises:
            DropboxAuthError: If no client is given and Dropbox is not authorized. Building
                makes no network call; it only checks that credentials are configured.

        """
        self._client = client if client is not None else DropboxClient()

    def create(self, path: str, content: bytes) -> FileInfo:
        """Create a file. Raise ``ConflictError`` if a file or folder is already at ``path``."""
        _check_path(path)
        with _shared_errors(), _local_copy(content) as local_path:
            created = self._client.upload_file(local_path, path, overwrite=False)
        return _file_info(created)

    def replace(self, path: str, content: bytes) -> FileInfo:
        """Replace a file's contents. Raise ``NotFoundError`` if it is missing; never create.

        Also raises ``InvalidOperationError`` for a folder, and ``ConflictError`` if the file
        changed while it was being replaced.
        """
        _check_path(path)
        with _shared_errors(), _local_copy(content) as local_path:
            replaced = self._client.replace_file(local_path, path)
        return _file_info(replaced)

    def download(self, path: str) -> bytes:
        """Return the exact bytes of the file at ``path``; a folder is ``InvalidOperationError``."""
        _check_path(path)
        with _shared_errors(), tempfile.TemporaryDirectory() as tmp_dir:
            local_path = Path(tmp_dir) / "download"
            self._client.download_file(path, str(local_path))
            return local_path.read_bytes()

    def get_metadata(self, path: str) -> FileInfo:
        """Return the info of the file or folder at ``path``. Raise ``NotFoundError`` if missing."""
        _check_path(path)
        with _shared_errors():
            return _file_info(self._client.get_metadata(path))

    def list_files(self, path: str) -> list[FileInfo]:
        """Return the direct children of the folder at ``path`` (``/`` is the root), sorted by path.

        Every provider page is fetched. Raises ``NotFoundError`` for a missing folder and
        ``InvalidOperationError`` if ``path`` is a file.
        """
        if path != _ROOT:
            _check_path(path)
        with _shared_errors():
            entries = list(self._client.list_folder("" if path == _ROOT else path))
        return sorted(
            (_file_info(entry, listed=True) for entry in entries),
            key=lambda info: info.path,
        )

    def move(self, from_path: str, to_path: str) -> FileInfo:
        """Move a file to ``to_path`` and return its new info.

        Follows the shared conformance suite: only files move (a folder is
        ``InvalidOperationError``), a ``to_path`` equal to ``from_path`` ignoring case is
        ``InvalidOperationError``, and an occupied ``to_path`` is ``ConflictError``. The source
        is read first to apply those rules.
        """
        _check_path(from_path)
        _check_path(to_path)
        with _shared_errors():
            if self._client.get_metadata(from_path).is_folder:
                msg = f"Cannot move {from_path!r}: it is a folder, and only files can be moved"
                raise InvalidOperationError(msg)
            if to_path.lower() == from_path.lower():
                msg = (
                    f"Cannot move {from_path!r} to {to_path!r}: it is the same path (ignoring case)"
                )
                raise InvalidOperationError(msg)
            return _file_info(self._client.move(from_path, to_path))

    def delete(self, path: str) -> None:
        """Delete a file, or a folder with everything in it. The root is never deleted."""
        if path == _ROOT:
            msg = "The root folder is never deleted"
            raise InvalidOperationError(msg)
        _check_path(path)
        with _shared_errors():
            self._client.delete(path)

    def check_health(self) -> None:
        """Make one lightweight Dropbox call. Raise ``ProviderUnavailableError`` if it fails."""
        try:
            self._client.check_health()
        except OSError as e:
            msg = "Dropbox is unreachable or refused the service's credentials"
            raise ProviderUnavailableError(msg) from e


def _check_path(path: str) -> None:
    """Raise ``InvalidRequestError`` unless ``path`` names an entry below the root.

    The shared path rule: an absolute path with a leading ``/``, not the root, not ending
    in ``/``, and with no empty, ``.`` or ``..`` segment.
    """
    if (
        not isinstance(path, str)
        or not path.startswith(_ROOT)
        or path == _ROOT
        or path.endswith("/")
    ):
        msg = (
            f"Invalid path {path!r}: it must start with '/', not be the root, and not end with '/'"
        )
        raise InvalidRequestError(msg)
    if any(part in {"", ".", ".."} for part in path[1:].split("/")):
        msg = f"Invalid path {path!r}: it must not contain empty, '.' or '..' segments"
        raise InvalidRequestError(msg)


@contextmanager
def _local_copy(content: bytes) -> Iterator[str]:
    """Write ``content`` to a temporary file and yield its path; the file is removed afterwards."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = Path(tmp_dir) / "upload"
        local_path.write_bytes(content)
        yield str(local_path)


@contextmanager
def _shared_errors() -> Iterator[None]:
    """Turn the ``Client`` contract's built-in exceptions into the shared storage errors.

    The client's message is kept: it names the paths involved and never contains
    credentials. More specific exceptions are checked first (each listed ``OSError``
    subclass before ``OSError``, and ``FileTooLargeError`` before ``ValueError``).
    """
    try:
        yield
    except cloud_storage_client_api.FileTooLargeError as e:
        raise FileTooLargeError(str(e)) from e
    except (ValueError, IsADirectoryError) as e:
        raise InvalidOperationError(str(e)) from e
    except FileNotFoundError as e:
        raise NotFoundError(str(e)) from e
    except FileExistsError as e:
        raise ConflictError(str(e)) from e
    except PermissionError as e:
        raise PermissionDeniedError(str(e)) from e
    except ConnectionError as e:
        raise ProviderUnavailableError(str(e)) from e
    except OSError as e:
        raise ProviderError(str(e)) from e


def _file_info(file: cloud_storage_client_api.File, *, listed: bool = False) -> FileInfo:
    """Convert a ``File`` into the shared ``FileInfo``. ``type`` is set on list entries only."""
    return FileInfo(
        id=file.id,
        name=file.name,
        path=file.path,
        size=file.size,
        modified_at=_utc_timestamp(file.modified),
        etag=file.etag,
        type=("folder" if file.is_folder else "file") if listed else None,
    )


def _utc_timestamp(moment: datetime | None) -> str | None:
    """Return ``moment`` as ISO 8601 in UTC ending in ``Z`` (``2026-11-15T12:00:00Z``)."""
    if moment is None:
        return None
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")
