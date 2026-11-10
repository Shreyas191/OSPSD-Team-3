"""Unit tests for ``DropboxClient.get_metadata``.

Covers the ``files_get_metadata`` call, the ``File`` returned for a file and for a folder,
and how each Dropbox failure becomes the exception the ``Client`` contract documents. The SDK
is an autospec fake, so no network or credentials are needed.
"""

from unittest.mock import MagicMock

import pytest
from dropbox import files
from tests.dropbox_sdk import (
    MODIFIED,
    api_error,
    file_metadata,
    folder_metadata,
    rate_limited,
    server_error,
)

from dropbox_client_impl import DropboxClient


def test_get_metadata_of_a_file(client: DropboxClient, dbx: MagicMock) -> None:
    """A file's metadata carries its name, path, id, size and modification time."""
    dbx.files_get_metadata.return_value = file_metadata("/data.csv", size=512)

    result = client.get_metadata("/data.csv")

    dbx.files_get_metadata.assert_called_once_with("/data.csv")
    assert (result.name, result.path, result.id) == ("data.csv", "/data.csv", "id:file123")
    assert result.is_folder is False
    assert result.size == 512
    assert result.modified == MODIFIED


def test_get_metadata_of_a_folder(client: DropboxClient, dbx: MagicMock) -> None:
    """A folder's metadata has no size or modification time."""
    dbx.files_get_metadata.return_value = folder_metadata("/Photos", folder_id="id:folder999")

    result = client.get_metadata("/Photos")

    dbx.files_get_metadata.assert_called_once_with("/Photos")
    assert (result.name, result.path, result.id) == ("Photos", "/Photos", "id:folder999")
    assert result.is_folder is True
    assert result.size is None
    assert result.modified is None


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (
            api_error(files.GetMetadataError.path(files.LookupError.not_found)),
            FileNotFoundError,
            "No file or folder at '/x'",
        ),
        (
            api_error(files.GetMetadataError.path(files.LookupError.restricted_content)),
            PermissionError,
            "Dropbox does not allow reading '/x'",
        ),
        (rate_limited(), ConnectionError, "Dropbox is unreachable or busy while reading '/x'"),
        (server_error(), OSError, "Dropbox failed reading '/x'"),
    ],
)
def test_get_metadata_translates_dropbox_errors(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
    message: str,
) -> None:
    """Each Dropbox metadata failure becomes the contract exception that names the path."""
    dbx.files_get_metadata.side_effect = error

    with pytest.raises(expected, match=message) as raised:
        client.get_metadata("/x")

    assert type(raised.value) is expected
