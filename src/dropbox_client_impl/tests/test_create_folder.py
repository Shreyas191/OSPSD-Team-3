"""Unit tests for ``DropboxClient.create_folder``.

The Dropbox SDK is an autospec mock (see ``conftest.py``) that returns and raises real SDK
objects, so each test checks the exact ``files_create_folder_v2`` call, the folder ``File``
built from Dropbox's result, and the documented exception each Dropbox failure becomes.
"""

from unittest.mock import MagicMock

import pytest
import requests
from dropbox import auth, exceptions, files, stone_validators
from tests.dropbox_sdk import api_error, folder_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient


def _write_failed(write: files.WriteError) -> exceptions.ApiError:
    """Return the error Dropbox raises when creating the folder fails with ``write``."""
    return api_error(files.CreateFolderError.path(write))


@pytest.mark.parametrize("path", ["/Documents", "/Parent/Sub/Target"])
def test_create_folder_returns_the_new_folder(
    client: DropboxClient,
    dbx: MagicMock,
    path: str,
) -> None:
    """Create_folder passes the path to Dropbox and returns the folder it made."""
    dbx.files_create_folder_v2.return_value = files.CreateFolderResult(
        metadata=folder_metadata(path, folder_id="id:folder101"),
    )

    created = client.create_folder(path)

    dbx.files_create_folder_v2.assert_called_once_with(path)
    assert created.path == path
    assert created.name == path.rsplit("/", 1)[-1]
    assert created.id == "id:folder101"
    assert created.is_folder is True
    assert created.size is None
    assert created.modified is None


def test_create_folder_on_existing_folder_raises_file_exists(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A Dropbox write conflict on folder creation becomes FileExistsError."""
    dbx.files_create_folder_v2.side_effect = _write_failed(
        files.WriteError.conflict(files.WriteConflictError.folder),
    )

    with pytest.raises(FileExistsError, match="Something already exists at '/Existing'"):
        client.create_folder("/Existing")


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
        # Folder-creation reasons.
        (_write_failed(files.WriteError.conflict(files.WriteConflictError.file)), FileExistsError),
        (_write_failed(files.WriteError.malformed_path("x")), ValueError),
        (_write_failed(files.WriteError.no_write_permission), PermissionError),
        (_write_failed(files.WriteError.too_many_write_operations), ConnectionError),
        (_write_failed(files.WriteError.insufficient_space), OSError),
    ],
)
def test_create_folder_errors_become_the_documented_exceptions(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes folder creation: each becomes one documented built-in."""
    dbx.files_create_folder_v2.side_effect = error

    with pytest.raises(expected) as raised:
        client.create_folder("/Docs/New")

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error
    assert "'/Docs/New'" in str(raised.value)


def test_create_folder_caller_facing_message_hides_dropbox_details(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A permission error's message, which callers see, does not expose Dropbox's error tag."""
    dbx.files_create_folder_v2.side_effect = _write_failed(files.WriteError.no_write_permission)

    with pytest.raises(PermissionError) as raised:
        client.create_folder("/Team/New")

    assert "'/Team/New'" in str(raised.value)
    assert "no_write_permission" not in str(raised.value)


def test_create_folder_provider_failure_message_names_the_dropbox_error(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A provider failure's message keeps the Dropbox error tag for the service's logs."""
    dbx.files_create_folder_v2.side_effect = _write_failed(files.WriteError.insufficient_space)

    with pytest.raises(OSError, match="insufficient_space"):
        client.create_folder("/Docs/New")
