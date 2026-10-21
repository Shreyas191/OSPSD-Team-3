"""Unit tests for DropboxClient.rename and DropboxClient.move.

The Dropbox SDK is mocked, so these tests need no network or credentials. Metadata
returned by the mock is a real SDK object so the File translation is exercised too.
"""

import re
from unittest.mock import Mock

import pytest
import requests
from dropbox import auth, exceptions, files, stone_validators

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


def test_rename_to_current_name_raises_file_exists(dbx: Mock) -> None:
    """Dropbox's duplicated_or_nested_paths (destination is the source) becomes FileExistsError."""
    dbx.files_move_v2.side_effect = _api_error(files.RelocationError.duplicated_or_nested_paths)

    with pytest.raises(FileExistsError, match=re.escape("/Docs/report.pdf")):
        DropboxClient(dbx=dbx).rename("/Docs/report.pdf", "report.pdf")


_Relocation = files.RelocationError


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # The request itself is invalid.
        (stone_validators.ValidationError("'relative' did not match pattern"), ValueError),
        (_api_error(_Relocation.from_lookup(files.LookupError.malformed_path("bad"))), ValueError),
        (_api_error(_Relocation.to(files.WriteError.disallowed_name)), ValueError),
        (_api_error(_Relocation.cant_move_folder_into_itself), ValueError),
        # Nothing to move, or the destination is taken.
        (_api_error(_Relocation.from_lookup(files.LookupError.not_found)), FileNotFoundError),
        (
            _api_error(_Relocation.to(files.WriteError.conflict(files.WriteConflictError.file))),
            FileExistsError,
        ),
        (_api_error(_Relocation.duplicated_or_nested_paths), FileExistsError),
        # Dropbox refuses this move for this account.
        (_api_error(_Relocation.from_lookup(files.LookupError.locked)), PermissionError),
        (_api_error(_Relocation.from_write(files.WriteError.no_write_permission)), PermissionError),
        (_api_error(_Relocation.to(files.WriteError.no_write_permission)), PermissionError),
        (_api_error(_Relocation.cant_move_shared_folder), PermissionError),
        (_api_error(_Relocation.cant_nest_shared_folder), PermissionError),
        (_api_error(_Relocation.cant_transfer_ownership), PermissionError),
        # Dropbox is unreachable or busy; retrying later may succeed.
        (_api_error(_Relocation.to(files.WriteError.too_many_write_operations)), ConnectionError),
        (exceptions.RateLimitError("req-1"), ConnectionError),
        (requests.exceptions.ConnectionError("connection refused"), ConnectionError),
        (requests.exceptions.ReadTimeout("read timed out"), ConnectionError),
        # Anything else is a provider failure.
        (_api_error(_Relocation.insufficient_quota), OSError),
        (_api_error(_Relocation.to(files.WriteError.insufficient_space)), OSError),
        (_api_error(_Relocation.too_many_files), OSError),
        (_api_error(_Relocation.internal_error), OSError),
        (_api_error(_Relocation.other), OSError),
        (exceptions.ApiError("req-1", "unexpected", None, None), OSError),
        (exceptions.InternalServerError("req-1", 500, "oops"), OSError),
        (exceptions.AuthError("req-1", auth.AuthError.expired_access_token), OSError),
    ],
)
def test_dropbox_errors_become_the_documented_exceptions(
    dbx: Mock,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes a move: each failure becomes one documented built-in."""
    dbx.files_move_v2.side_effect = error

    with pytest.raises(expected) as raised:
        DropboxClient(dbx=dbx).move("/Docs/report.pdf", "/Archive/report.pdf")

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    ("from_path", "to_path"),
    [("/Docs", "/Docs/Inner"), ("/Docs", "/docs/inner/deeper"), ("/Docs/", "/Docs/Inner")],
)
def test_folder_moved_into_itself_is_rejected_without_calling_dropbox(
    dbx: Mock,
    from_path: str,
    to_path: str,
) -> None:
    """A destination inside the source (compared case-insensitively) is a ValueError.

    Dropbox reports this with the same ``duplicated_or_nested_paths`` error as a move onto
    the source's own path, so it is caught before the call.
    """
    with pytest.raises(ValueError, match="inside the source"):
        DropboxClient(dbx=dbx).move(from_path, to_path)

    dbx.files_move_v2.assert_not_called()


def test_sibling_with_a_shared_prefix_is_not_nested(dbx: Mock) -> None:
    """``/Docs2`` is not inside ``/Docs``, so the move goes to Dropbox."""
    DropboxClient(dbx=dbx).move("/Docs", "/Docs2")

    dbx.files_move_v2.assert_called_once_with("/Docs", "/Docs2", autorename=False)


def test_rename_translates_failures_like_move(dbx: Mock) -> None:
    """Rename goes through move, so its provider failures are translated the same way."""
    dbx.files_move_v2.side_effect = exceptions.RateLimitError("req-1")

    with pytest.raises(ConnectionError, match=re.escape("'/Docs/report.pdf'")):
        DropboxClient(dbx=dbx).rename("/Docs/report.pdf", "summary.pdf")


def test_caller_facing_messages_hide_dropbox_details(dbx: Mock) -> None:
    """A permission error's message, which callers see, does not expose Dropbox's error tag."""
    dbx.files_move_v2.side_effect = _api_error(_Relocation.cant_move_shared_folder)

    with pytest.raises(PermissionError) as raised:
        DropboxClient(dbx=dbx).move("/Shared", "/Archive/Shared")

    assert str(raised.value) == "Dropbox does not allow moving '/Shared' to '/Archive/Shared'"


def test_provider_failure_message_names_the_dropbox_error(dbx: Mock) -> None:
    """A provider failure's message keeps the Dropbox error tag for the service's logs."""
    dbx.files_move_v2.side_effect = _api_error(_Relocation.insufficient_quota)

    with pytest.raises(OSError, match="insufficient_quota"):
        DropboxClient(dbx=dbx).move("/Docs/big.zip", "/Archive/big.zip")
