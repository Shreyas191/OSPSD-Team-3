"""Unit tests for ``DropboxClient.delete``.

The Dropbox SDK is an autospec mock (see ``conftest.py``) that returns and raises real SDK
objects, so each test checks the exact ``files_delete_v2`` call, that Dropbox is not
contacted for the root, and the documented exception each Dropbox failure becomes.
"""

import re
from unittest.mock import MagicMock

import pytest
import requests
from dropbox import auth, exceptions, files, stone_validators
from tests.dropbox_sdk import api_error, file_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient


def _lookup_error(reason: files.LookupError) -> exceptions.ApiError:
    return api_error(files.DeleteError.path_lookup(reason))


def _write_error(reason: files.WriteError) -> exceptions.ApiError:
    return api_error(files.DeleteError.path_write(reason))


def test_delete_calls_dropbox_and_returns_true(client: DropboxClient, dbx: MagicMock) -> None:
    """Delete passes the path to ``files_delete_v2`` and reports success."""
    dbx.files_delete_v2.return_value = files.DeleteResult(metadata=file_metadata())

    assert client.delete("/Docs/report.pdf") is True

    dbx.files_delete_v2.assert_called_once_with("/Docs/report.pdf")


@pytest.mark.parametrize("path", ["", "/", "//"])
def test_delete_root_is_rejected_without_calling_dropbox(
    client: DropboxClient,
    dbx: MagicMock,
    path: str,
) -> None:
    """Empty and root paths raise ValueError and never reach Dropbox."""
    with pytest.raises(ValueError, match="root"):
        client.delete(path)

    dbx.files_delete_v2.assert_not_called()


def test_delete_missing_path_raises_file_not_found(client: DropboxClient, dbx: MagicMock) -> None:
    """A Dropbox ``path_lookup.not_found`` error becomes FileNotFoundError."""
    dbx.files_delete_v2.side_effect = _lookup_error(files.LookupError.not_found)

    with pytest.raises(FileNotFoundError, match=re.escape("/Docs/missing.pdf")):
        client.delete("/Docs/missing.pdf")


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
        (api_error(files.DeleteError.too_many_write_operations), ConnectionError),
        (_write_error(files.WriteError.too_many_write_operations), ConnectionError),
        (rate_limited(), ConnectionError),
        (requests.exceptions.ConnectionError("connection refused"), ConnectionError),
        (requests.exceptions.ReadTimeout("read timed out"), ConnectionError),
        # Anything else is a provider failure.
        (api_error(files.DeleteError.too_many_files), OSError),
        (api_error(files.DeleteError.other), OSError),
        (api_error("unexpected"), OSError),
        (_lookup_error(files.LookupError.unsupported_content_type), OSError),
        (_write_error(files.WriteError.insufficient_space), OSError),
        (server_error(), OSError),
        (exceptions.AuthError("request-id", auth.AuthError.expired_access_token), OSError),
        (exceptions.BadInputError("request-id", "bad request"), OSError),
    ],
)
def test_dropbox_errors_become_the_documented_exceptions(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes: each failure becomes exactly one documented built-in."""
    dbx.files_delete_v2.side_effect = error

    with pytest.raises(expected) as raised:
        client.delete("/Docs/report.pdf")

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error
    assert "'/Docs/report.pdf'" in str(raised.value)


def test_caller_facing_messages_hide_dropbox_details(client: DropboxClient, dbx: MagicMock) -> None:
    """A permission error's message, which callers see, does not expose Dropbox's error tag."""
    dbx.files_delete_v2.side_effect = _write_error(files.WriteError.no_write_permission)

    with pytest.raises(PermissionError) as raised:
        client.delete("/Team")

    assert str(raised.value) == "Dropbox does not allow deleting '/Team'"


def test_provider_failure_message_names_the_dropbox_error(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A provider failure's message keeps the Dropbox error tag for the service's logs."""
    dbx.files_delete_v2.side_effect = api_error(files.DeleteError.too_many_files)

    with pytest.raises(OSError, match="too_many_files"):
        client.delete("/Huge")
