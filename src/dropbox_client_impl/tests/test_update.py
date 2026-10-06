"""Unit tests for DropboxClient.rename and DropboxClient.move.

The Dropbox SDK is mocked, so these tests need no network or credentials. Metadata
returned by the mock is a real SDK object so the File translation is exercised too.
"""

import re
from unittest.mock import Mock

import pytest
from dropbox import exceptions, files

from dropbox_client_impl import DropboxClient


def _folder(path: str) -> files.FolderMetadata:
    return files.FolderMetadata(
        name=path.rsplit("/", 1)[-1],
        id="id:abc",
        path_lower=path.lower(),
        path_display=path,
    )


def _api_error(error: files.RelocationError) -> exceptions.ApiError:
    return exceptions.ApiError("req-1", error, None, None)


@pytest.fixture
def dbx() -> Mock:
    """Return a mocked Dropbox SDK whose move succeeds with the destination's metadata."""
    sdk = Mock()
    sdk.files_move_v2.side_effect = lambda _from, to, **_: files.RelocationResult(
        metadata=_folder(to),
    )
    return sdk


def test_move_returns_moved_entry(dbx: Mock) -> None:
    """Move relocates the entry and returns its metadata at the new path."""
    moved = DropboxClient(dbx=dbx).move("/Docs/Reports", "/Archive/Reports")

    dbx.files_move_v2.assert_called_once_with("/Docs/Reports", "/Archive/Reports", autorename=False)
    assert moved.path == "/Archive/Reports"
    assert moved.name == "Reports"
    assert moved.is_folder is True


@pytest.mark.parametrize(
    ("path", "new_name", "expected"),
    [
        ("/Docs/report.pdf", "summary.pdf", "/Docs/summary.pdf"),
        ("/report.pdf", "summary.pdf", "/summary.pdf"),
        ("/Docs/Reports/", "Old Reports", "/Docs/Old Reports"),
    ],
)
def test_rename_keeps_parent_folder(dbx: Mock, path: str, new_name: str, expected: str) -> None:
    """Rename only changes the last path segment."""
    renamed = DropboxClient(dbx=dbx).rename(path, new_name)

    dbx.files_move_v2.assert_called_once_with(path, expected, autorename=False)
    assert renamed.path == expected
    assert renamed.name == new_name


@pytest.mark.parametrize("new_name", ["", "a/b"])
def test_rename_rejects_invalid_name(dbx: Mock, new_name: str) -> None:
    """Names that are empty or contain a slash never reach Dropbox."""
    with pytest.raises(ValueError, match="Invalid name"):
        DropboxClient(dbx=dbx).rename("/Docs/report.pdf", new_name)

    dbx.files_move_v2.assert_not_called()


def test_move_missing_source_raises_file_not_found(dbx: Mock) -> None:
    """A Dropbox not_found lookup error becomes FileNotFoundError."""
    dbx.files_move_v2.side_effect = _api_error(
        files.RelocationError.from_lookup(files.LookupError.not_found),
    )

    with pytest.raises(FileNotFoundError, match=re.escape("/missing.txt")):
        DropboxClient(dbx=dbx).move("/missing.txt", "/b.txt")


def test_rename_onto_existing_name_raises_file_exists(dbx: Mock) -> None:
    """A Dropbox write conflict at the destination becomes FileExistsError."""
    conflict = files.WriteError.conflict(files.WriteConflictError.file)
    dbx.files_move_v2.side_effect = _api_error(files.RelocationError.to(conflict))

    with pytest.raises(FileExistsError, match=re.escape("/Docs/taken.pdf")):
        DropboxClient(dbx=dbx).rename("/Docs/report.pdf", "taken.pdf")


def test_other_dropbox_errors_propagate(dbx: Mock) -> None:
    """Errors with no domain meaning are not disguised as missing or conflicting files."""
    error = _api_error(files.RelocationError.cant_move_folder_into_itself)
    dbx.files_move_v2.side_effect = error

    with pytest.raises(exceptions.ApiError) as raised:
        DropboxClient(dbx=dbx).move("/Docs", "/Docs/Inner")

    assert raised.value is error


def test_non_relocation_api_error_propagates(dbx: Mock) -> None:
    """An ApiError carrying an unexpected error type is re-raised unchanged."""
    error = exceptions.ApiError("req-1", "unexpected", None, None)
    dbx.files_move_v2.side_effect = error

    with pytest.raises(exceptions.ApiError) as raised:
        DropboxClient(dbx=dbx).move("/a.txt", "/b.txt")

    assert raised.value is error
