"""Unit tests for ``DropboxClient.rename`` and ``DropboxClient.move``.

The Dropbox SDK is an autospec mock (see ``conftest.py``) whose ``files_move_v2`` returns a
real ``RelocationResult`` for the destination, so each test checks the exact SDK call, the
``File`` built from Dropbox's metadata, and the documented exception each failure becomes.
"""

import re
from unittest.mock import MagicMock

import pytest
import requests
from dropbox import auth, exceptions, files, stone_validators
from tests.dropbox_sdk import api_error, folder_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient


@pytest.fixture(name="dbx")
def moving_dbx(dbx: MagicMock) -> MagicMock:
    """Return the fake SDK, with a move that succeeds with the destination's metadata."""
    dbx.files_move_v2.side_effect = lambda _from, to, **_: files.RelocationResult(
        metadata=folder_metadata(to),
    )
    return dbx


def test_move_returns_moved_entry(client: DropboxClient, dbx: MagicMock) -> None:
    """Move relocates the entry and returns its metadata at the new path."""
    moved = client.move("/Docs/Reports", "/Archive/Reports")

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
def test_rename_keeps_parent_folder(
    client: DropboxClient,
    dbx: MagicMock,
    path: str,
    new_name: str,
    expected: str,
) -> None:
    """Rename only changes the last path segment."""
    renamed = client.rename(path, new_name)

    dbx.files_move_v2.assert_called_once_with(path, expected, autorename=False)
    assert renamed.path == expected
    assert renamed.name == new_name


@pytest.mark.parametrize("new_name", ["", "a/b"])
def test_rename_rejects_invalid_name(client: DropboxClient, dbx: MagicMock, new_name: str) -> None:
    """Names that are empty or contain a slash never reach Dropbox."""
    with pytest.raises(ValueError, match="Invalid name"):
        client.rename("/Docs/report.pdf", new_name)

    dbx.files_move_v2.assert_not_called()


def test_move_missing_source_raises_file_not_found(client: DropboxClient, dbx: MagicMock) -> None:
    """A Dropbox not_found lookup error becomes FileNotFoundError."""
    dbx.files_move_v2.side_effect = api_error(
        files.RelocationError.from_lookup(files.LookupError.not_found),
    )

    with pytest.raises(FileNotFoundError, match=re.escape("/missing.txt")):
        client.move("/missing.txt", "/b.txt")


def test_rename_onto_existing_name_raises_file_exists(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A Dropbox write conflict at the destination becomes FileExistsError."""
    conflict = files.WriteError.conflict(files.WriteConflictError.file)
    dbx.files_move_v2.side_effect = api_error(files.RelocationError.to(conflict))

    with pytest.raises(FileExistsError, match=re.escape("/Docs/taken.pdf")):
        client.rename("/Docs/report.pdf", "taken.pdf")


def test_rename_to_current_name_raises_file_exists(client: DropboxClient, dbx: MagicMock) -> None:
    """Dropbox's duplicated_or_nested_paths (destination is the source) becomes FileExistsError."""
    dbx.files_move_v2.side_effect = api_error(files.RelocationError.duplicated_or_nested_paths)

    with pytest.raises(FileExistsError, match=re.escape("/Docs/report.pdf")):
        client.rename("/Docs/report.pdf", "report.pdf")


_Relocation = files.RelocationError


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # The request itself is invalid.
        (stone_validators.ValidationError("'relative' did not match pattern"), ValueError),
        (api_error(_Relocation.from_lookup(files.LookupError.malformed_path("bad"))), ValueError),
        (api_error(_Relocation.to(files.WriteError.disallowed_name)), ValueError),
        (api_error(_Relocation.cant_move_folder_into_itself), ValueError),
        # Nothing to move, or the destination is taken.
        (api_error(_Relocation.from_lookup(files.LookupError.not_found)), FileNotFoundError),
        (
            api_error(_Relocation.to(files.WriteError.conflict(files.WriteConflictError.file))),
            FileExistsError,
        ),
        (api_error(_Relocation.duplicated_or_nested_paths), FileExistsError),
        # Dropbox refuses this move for this account.
        (api_error(_Relocation.from_lookup(files.LookupError.locked)), PermissionError),
        (api_error(_Relocation.from_write(files.WriteError.no_write_permission)), PermissionError),
        (api_error(_Relocation.to(files.WriteError.no_write_permission)), PermissionError),
        (api_error(_Relocation.cant_move_shared_folder), PermissionError),
        (api_error(_Relocation.cant_nest_shared_folder), PermissionError),
        (api_error(_Relocation.cant_transfer_ownership), PermissionError),
        # Dropbox is unreachable or busy; retrying later may succeed.
        (api_error(_Relocation.to(files.WriteError.too_many_write_operations)), ConnectionError),
        (rate_limited(), ConnectionError),
        (requests.exceptions.ConnectionError("connection refused"), ConnectionError),
        (requests.exceptions.ReadTimeout("read timed out"), ConnectionError),
        # Anything else is a provider failure.
        (api_error(_Relocation.insufficient_quota), OSError),
        (api_error(_Relocation.to(files.WriteError.insufficient_space)), OSError),
        (api_error(_Relocation.too_many_files), OSError),
        (api_error(_Relocation.internal_error), OSError),
        (api_error(_Relocation.other), OSError),
        (api_error("unexpected"), OSError),
        (server_error(), OSError),
        (exceptions.AuthError("request-id", auth.AuthError.expired_access_token), OSError),
    ],
)
def test_dropbox_errors_become_the_documented_exceptions(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes a move: each failure becomes one documented built-in."""
    dbx.files_move_v2.side_effect = error

    with pytest.raises(expected) as raised:
        client.move("/Docs/report.pdf", "/Archive/report.pdf")

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    ("from_path", "to_path"),
    [("/Docs", "/Docs/Inner"), ("/Docs", "/docs/inner/deeper"), ("/Docs/", "/Docs/Inner")],
)
def test_folder_moved_into_itself_is_rejected_without_calling_dropbox(
    client: DropboxClient,
    dbx: MagicMock,
    from_path: str,
    to_path: str,
) -> None:
    """A destination inside the source (compared case-insensitively) is a ValueError.

    Dropbox reports this with the same ``duplicated_or_nested_paths`` error as a move onto
    the source's own path, so it is caught before the call.
    """
    with pytest.raises(ValueError, match="inside the source"):
        client.move(from_path, to_path)

    dbx.files_move_v2.assert_not_called()


def test_sibling_with_a_shared_prefix_is_not_nested(client: DropboxClient, dbx: MagicMock) -> None:
    """``/Docs2`` is not inside ``/Docs``, so the move goes to Dropbox."""
    client.move("/Docs", "/Docs2")

    dbx.files_move_v2.assert_called_once_with("/Docs", "/Docs2", autorename=False)


def test_rename_translates_failures_like_move(client: DropboxClient, dbx: MagicMock) -> None:
    """Rename goes through move, so its provider failures are translated the same way."""
    dbx.files_move_v2.side_effect = rate_limited()

    with pytest.raises(ConnectionError, match=re.escape("'/Docs/report.pdf'")):
        client.rename("/Docs/report.pdf", "summary.pdf")


def test_caller_facing_messages_hide_dropbox_details(client: DropboxClient, dbx: MagicMock) -> None:
    """A permission error's message, which callers see, does not expose Dropbox's error tag."""
    dbx.files_move_v2.side_effect = api_error(_Relocation.cant_move_shared_folder)

    with pytest.raises(PermissionError) as raised:
        client.move("/Shared", "/Archive/Shared")

    assert str(raised.value) == "Dropbox does not allow moving '/Shared' to '/Archive/Shared'"


def test_provider_failure_message_names_the_dropbox_error(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A provider failure's message keeps the Dropbox error tag for the service's logs."""
    dbx.files_move_v2.side_effect = api_error(_Relocation.insufficient_quota)

    with pytest.raises(OSError, match="insufficient_quota"):
        client.move("/Docs/big.zip", "/Archive/big.zip")
