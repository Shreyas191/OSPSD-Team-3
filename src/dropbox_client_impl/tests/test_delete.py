"""Unit tests for DropboxClient.delete.

The Dropbox SDK is mocked, so these tests need no network or credentials.
"""

import re
from unittest.mock import Mock

import pytest
import requests
from dropbox import auth, exceptions, files, stone_validators

from dropbox_client_impl import DropboxClient


def _api_error(error: object) -> exceptions.ApiError:
    return exceptions.ApiError("req-1", error, None, None)


def _lookup_error(reason: files.LookupError) -> exceptions.ApiError:
    return _api_error(files.DeleteError.path_lookup(reason))


def _write_error(reason: files.WriteError) -> exceptions.ApiError:
    return _api_error(files.DeleteError.path_write(reason))


@pytest.fixture
def dbx() -> Mock:
    """Return a mocked Dropbox SDK whose delete succeeds."""
    sdk = Mock()
    deleted = files.FileMetadata(name="report.pdf", id="id:abc", path_display="/Docs/report.pdf")
    sdk.files_delete_v2.return_value = files.DeleteResult(metadata=deleted)
    return sdk


def test_delete_calls_dropbox_and_returns_true(dbx: Mock) -> None:
    """Delete passes the path to ``files_delete_v2`` and reports success."""
    assert DropboxClient(dbx=dbx).delete("/Docs/report.pdf") is True

    dbx.files_delete_v2.assert_called_once_with("/Docs/report.pdf")


@pytest.mark.parametrize("path", ["", "/", "//"])
def test_delete_root_is_rejected_without_calling_dropbox(dbx: Mock, path: str) -> None:
    """Empty and root paths raise ValueError and never reach Dropbox."""
    with pytest.raises(ValueError, match="root"):
        DropboxClient(dbx=dbx).delete(path)

    dbx.files_delete_v2.assert_not_called()


def test_delete_missing_path_raises_file_not_found(dbx: Mock) -> None:
    """A Dropbox ``path_lookup.not_found`` error becomes FileNotFoundError."""
    dbx.files_delete_v2.side_effect = _lookup_error(files.LookupError.not_found)

    with pytest.raises(FileNotFoundError, match=re.escape("/Docs/missing.pdf")):
        DropboxClient(dbx=dbx).delete("/Docs/missing.pdf")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # The path itself is unusable.
        (stone_validators.ValidationError("'relative' did not match pattern"), ValueError),
        (_lookup_error(files.LookupError.malformed_path("bad")), ValueError),
        (_write_error(files.WriteError.malformed_path("bad")), ValueError),
        (_write_error(files.WriteError.disallowed_name), ValueError),
        # Dropbox refuses to delete this entry for this account.
        (_lookup_error(files.LookupError.restricted_content), PermissionError),
        (_lookup_error(files.LookupError.locked), PermissionError),
        (_write_error(files.WriteError.no_write_permission), PermissionError),
        (_write_error(files.WriteError.team_folder), PermissionError),
        (_write_error(files.WriteError.access_restricted), PermissionError),
        (_write_error(files.WriteError.operation_suppressed), PermissionError),
        # Dropbox is unreachable or busy; retrying later may succeed.
        (_api_error(files.DeleteError.too_many_write_operations), ConnectionError),
        (_write_error(files.WriteError.too_many_write_operations), ConnectionError),
        (exceptions.RateLimitError("req-1"), ConnectionError),
        (requests.exceptions.ConnectionError("connection refused"), ConnectionError),
        (requests.exceptions.ReadTimeout("read timed out"), ConnectionError),
        # Anything else is a provider failure.
        (_api_error(files.DeleteError.too_many_files), OSError),
        (_api_error(files.DeleteError.other), OSError),
        (_api_error("unexpected"), OSError),
        (_lookup_error(files.LookupError.unsupported_content_type), OSError),
        (_write_error(files.WriteError.insufficient_space), OSError),
        (exceptions.InternalServerError("req-1", 500, "oops"), OSError),
        (exceptions.AuthError("req-1", auth.AuthError.expired_access_token), OSError),
        (exceptions.BadInputError("req-1", "bad request"), OSError),
    ],
)
def test_dropbox_errors_become_the_documented_exceptions(
    dbx: Mock,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes: each failure becomes exactly one documented built-in."""
    dbx.files_delete_v2.side_effect = error

    with pytest.raises(expected) as raised:
        DropboxClient(dbx=dbx).delete("/Docs/report.pdf")

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error
    assert "'/Docs/report.pdf'" in str(raised.value)


def test_caller_facing_messages_hide_dropbox_details(dbx: Mock) -> None:
    """A permission error's message, which callers see, does not expose Dropbox's error tag."""
    dbx.files_delete_v2.side_effect = _write_error(files.WriteError.no_write_permission)

    with pytest.raises(PermissionError) as raised:
        DropboxClient(dbx=dbx).delete("/Team")

    assert str(raised.value) == "Dropbox does not allow deleting '/Team'"


def test_provider_failure_message_names_the_dropbox_error(dbx: Mock) -> None:
    """A provider failure's message keeps the Dropbox error tag for the service's logs."""
    dbx.files_delete_v2.side_effect = _api_error(files.DeleteError.too_many_files)

    with pytest.raises(OSError, match="too_many_files"):
        DropboxClient(dbx=dbx).delete("/Huge")
