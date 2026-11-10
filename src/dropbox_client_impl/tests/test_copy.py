"""Unit tests for ``DropboxClient.copy``.

The Dropbox SDK is an autospec mock (see ``conftest.py``) that returns and raises real SDK
objects, so each test checks the exact ``files_copy_v2`` call, the ``File`` built from the
copy's metadata, and the documented exception each Dropbox failure becomes.
"""

from unittest.mock import MagicMock

import pytest
import requests
from dropbox import auth, exceptions, files, stone_validators
from tests.dropbox_sdk import api_error, file_metadata, folder_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient

_Relocation = files.RelocationError


def test_copy_file_returns_the_copy(client: DropboxClient, dbx: MagicMock) -> None:
    """Copying a file calls files_copy_v2 without autorename and returns the copy."""
    copied = file_metadata("/Backup/report.pdf", file_id="id:copy123", size=4096)
    dbx.files_copy_v2.return_value = files.RelocationResult(metadata=copied)

    result = client.copy("/Docs/report.pdf", "/Backup/report.pdf")

    dbx.files_copy_v2.assert_called_once_with(
        "/Docs/report.pdf",
        "/Backup/report.pdf",
        autorename=False,
    )
    assert result.path == "/Backup/report.pdf"
    assert result.name == "report.pdf"
    assert result.id == "id:copy123"
    assert result.is_folder is False
    assert result.size == 4096
    assert result.modified == copied.server_modified


def test_copy_folder_returns_the_copied_folder(client: DropboxClient, dbx: MagicMock) -> None:
    """Copying a folder uses the same files_copy_v2 call and returns the copied folder."""
    copied = folder_metadata("/Archives/Project", folder_id="id:folder789")
    dbx.files_copy_v2.return_value = files.RelocationResult(metadata=copied)

    result = client.copy("/Active/Project", "/Archives/Project")

    dbx.files_copy_v2.assert_called_once_with(
        "/Active/Project",
        "/Archives/Project",
        autorename=False,
    )
    assert result.path == "/Archives/Project"
    assert result.name == "Project"
    assert result.id == "id:folder789"
    assert result.is_folder is True
    assert result.size is None
    assert result.modified is None


@pytest.mark.parametrize(
    ("error", "expected", "fragment"),
    [
        (_Relocation.from_lookup(files.LookupError.not_found), FileNotFoundError, "'/a.txt'"),
        (
            _Relocation.to(files.WriteError.conflict(files.WriteConflictError.file)),
            FileExistsError,
            "Something already exists at '/b.txt'",
        ),
    ],
)
def test_copy_missing_source_or_taken_destination(
    client: DropboxClient,
    dbx: MagicMock,
    error: files.RelocationError,
    expected: type[Exception],
    fragment: str,
) -> None:
    """A missing source is FileNotFoundError and a taken destination is FileExistsError."""
    dbx.files_copy_v2.side_effect = api_error(error)

    with pytest.raises(expected, match=fragment):
        client.copy("/a.txt", "/b.txt")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # Failures every SDK call can hit.
        (stone_validators.ValidationError("'relative' did not match pattern"), ValueError),
        (rate_limited(), ConnectionError),
        (requests.exceptions.ConnectionError("connection refused"), ConnectionError),
        (requests.exceptions.ReadTimeout("read timed out"), ConnectionError),
        (server_error(), OSError),
        (exceptions.AuthError("request-id", auth.AuthError.expired_access_token), OSError),
        (api_error("unexpected"), OSError),
        # Copy-specific reasons.
        (api_error(_Relocation.from_lookup(files.LookupError.not_found)), FileNotFoundError),
        (
            api_error(_Relocation.to(files.WriteError.conflict(files.WriteConflictError.file))),
            FileExistsError,
        ),
        (api_error(_Relocation.duplicated_or_nested_paths), FileExistsError),
        (api_error(_Relocation.from_lookup(files.LookupError.malformed_path("x"))), ValueError),
        (api_error(_Relocation.to(files.WriteError.no_write_permission)), PermissionError),
        (api_error(_Relocation.cant_copy_shared_folder), PermissionError),
        (api_error(_Relocation.to(files.WriteError.too_many_write_operations)), ConnectionError),
        (api_error(_Relocation.insufficient_quota), OSError),
        (api_error(_Relocation.too_many_files), OSError),
    ],
)
def test_copy_errors_become_the_documented_exceptions(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes a copy: each failure becomes one documented built-in."""
    dbx.files_copy_v2.side_effect = error

    with pytest.raises(expected) as raised:
        client.copy("/Docs/report.pdf", "/Archive/report.pdf")

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error
    assert "report.pdf'" in str(raised.value)


@pytest.mark.parametrize(
    ("from_path", "to_path"),
    [("/work", "/work/nested"), ("/Work", "/work/nested/deeper")],
)
def test_copy_folder_into_itself_is_rejected_without_calling_dropbox(
    client: DropboxClient,
    dbx: MagicMock,
    from_path: str,
    to_path: str,
) -> None:
    """A destination inside the source (compared case-insensitively) is a ValueError."""
    with pytest.raises(ValueError, match="into itself"):
        client.copy(from_path, to_path)

    dbx.files_copy_v2.assert_not_called()


def test_copy_caller_facing_message_hides_dropbox_details(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A permission error's message, which callers see, does not expose Dropbox's error tag."""
    dbx.files_copy_v2.side_effect = api_error(_Relocation.cant_copy_shared_folder)

    with pytest.raises(PermissionError) as raised:
        client.copy("/Shared", "/Archive/Shared")

    assert "Dropbox does not allow copying '/Shared' to '/Archive/Shared'" in str(raised.value)
    assert "cant_copy_shared_folder" not in str(raised.value)


def test_copy_provider_failure_message_names_the_dropbox_error(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A provider failure's message keeps the Dropbox error tag for the service's logs."""
    dbx.files_copy_v2.side_effect = api_error(_Relocation.insufficient_quota)

    with pytest.raises(OSError, match="insufficient_quota"):
        client.copy("/Docs/big.zip", "/Archive/big.zip")
