"""Unit tests for ``DropboxClient.download_file``.

Covers the ``files_download_to_file`` call the client makes, the ``File`` it returns, and how
each Dropbox failure becomes the exception the ``Client`` contract documents. The SDK is an
autospec fake, so no network or credentials are needed.
"""

from unittest.mock import MagicMock

import pytest
from dropbox import files
from tests.dropbox_sdk import MODIFIED, api_error, file_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient


def test_download_file_returns_the_downloaded_file(client: DropboxClient, dbx: MagicMock) -> None:
    """A download passes both paths to the SDK and returns the file's metadata."""
    dbx.files_download_to_file.return_value = file_metadata("/Docs/report.pdf")

    result = client.download_file("/Docs/report.pdf", "/local/report.pdf")

    dbx.files_download_to_file.assert_called_once_with(
        download_path="/local/report.pdf",
        path="/Docs/report.pdf",
    )
    assert (result.name, result.path, result.id) == ("report.pdf", "/Docs/report.pdf", "id:file123")
    assert result.is_folder is False
    assert result.size == 2048
    assert result.modified == MODIFIED


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (
            api_error(files.DownloadError.path(files.LookupError.not_found)),
            FileNotFoundError,
            "No file or folder at '/x'",
        ),
        (
            api_error(files.DownloadError.path(files.LookupError.not_file)),
            IsADirectoryError,
            "Path is a folder: '/x'",
        ),
        (
            api_error(files.DownloadError.path(files.LookupError.restricted_content)),
            PermissionError,
            "Dropbox does not allow reading '/x'",
        ),
        (rate_limited(), ConnectionError, "Dropbox is unreachable or busy while reading '/x'"),
        (server_error(), OSError, "Dropbox failed reading '/x'"),
    ],
)
def test_download_file_translates_dropbox_errors(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
    message: str,
) -> None:
    """Each Dropbox download failure becomes the contract exception that names the path."""
    dbx.files_download_to_file.side_effect = error

    with pytest.raises(expected, match=message) as raised:
        client.download_file("/x", "/local/x")

    assert type(raised.value) is expected
