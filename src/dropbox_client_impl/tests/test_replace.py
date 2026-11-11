"""Unit tests for ``DropboxClient.replace_file``.

The Dropbox SDK is an autospec mock (see ``conftest.py``) that returns and raises real SDK
objects. Each test checks both SDK calls (``files_get_metadata`` for the current revision,
then ``files_upload`` with ``WriteMode.update``) and the documented exception each failure
becomes.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from dropbox import exceptions, files
from dropbox.files import WriteMode
from tests.dropbox_sdk import api_error, file_metadata, folder_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient

REV = "0a1b2c3d4e5f"


def _write_failed(write: files.WriteError) -> exceptions.ApiError:
    """Return the error Dropbox raises when writing the uploaded file fails with ``write``."""
    return api_error(
        files.UploadError.path(files.UploadWriteFailed(reason=write, upload_session_id="s")),
    )


@pytest.fixture
def local_file(tmp_path: Path) -> Path:
    """Return a small local file holding the new content."""
    path = tmp_path / "a.txt"
    path.write_bytes(b"new")
    return path


@pytest.fixture
def existing(dbx: MagicMock) -> files.FileMetadata:
    """Make ``/Docs/a.txt`` exist in the fake Dropbox at revision ``REV``."""
    metadata = file_metadata("/Docs/a.txt", file_id="id:a", size=3, rev=REV)
    dbx.files_get_metadata.return_value = metadata
    return metadata


def test_replace_reads_the_revision_then_uploads_against_it(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
    existing: files.FileMetadata,
) -> None:
    """Replace reads the current rev, uploads only against that rev, and keeps the file's id."""
    dbx.files_upload.return_value = file_metadata(
        "/Docs/a.txt",
        file_id=existing.id,
        size=3,
        rev="0fffffffff",
    )

    replaced = client.replace_file(str(local_file), "/Docs/a.txt")

    dbx.files_get_metadata.assert_called_once_with("/Docs/a.txt")
    dbx.files_upload.assert_called_once_with(
        b"new",
        "/Docs/a.txt",
        mode=WriteMode.update(REV),
        autorename=False,
    )
    assert replaced.id == "id:a"
    assert replaced.path == "/Docs/a.txt"
    assert replaced.size == 3
    assert replaced.is_folder is False


def test_replace_missing_file_never_creates_it(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
) -> None:
    """A missing remote file raises FileNotFoundError and nothing is uploaded."""
    dbx.files_get_metadata.side_effect = api_error(
        files.GetMetadataError.path(files.LookupError.not_found),
    )

    with pytest.raises(FileNotFoundError, match=r"'/Docs/a\.txt'"):
        client.replace_file(str(local_file), "/Docs/a.txt")

    dbx.files_upload.assert_not_called()


def test_replace_folder_is_rejected(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
) -> None:
    """A folder at the path raises IsADirectoryError and nothing is uploaded."""
    dbx.files_get_metadata.return_value = folder_metadata("/Docs/a.txt")

    with pytest.raises(IsADirectoryError, match=r"'/Docs/a\.txt'"):
        client.replace_file(str(local_file), "/Docs/a.txt")

    dbx.files_upload.assert_not_called()


@pytest.mark.usefixtures("existing")
def test_replace_stale_revision_is_file_exists(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
) -> None:
    """A file changed since its rev was read is a FileExistsError that says it changed."""
    stale = _write_failed(files.WriteError.conflict(files.WriteConflictError.file))
    dbx.files_upload.side_effect = stale

    with pytest.raises(FileExistsError, match="changed while it was being replaced") as raised:
        client.replace_file(str(local_file), "/Docs/a.txt")

    assert raised.value.__cause__ is stale
    assert "'/Docs/a.txt'" in str(raised.value)


@pytest.mark.usefixtures("existing")
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (api_error(files.UploadError.payload_too_large), ValueError),
        (_write_failed(files.WriteError.no_write_permission), PermissionError),
        (rate_limited(), ConnectionError),
        (requests.exceptions.ConnectionError("connection refused"), ConnectionError),
        (server_error(), OSError),
        (_write_failed(files.WriteError.insufficient_space), OSError),
    ],
)
def test_replace_upload_errors_become_the_documented_exceptions(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes the upload step: each becomes one documented built-in."""
    dbx.files_upload.side_effect = error

    with pytest.raises(expected) as raised:
        client.replace_file(str(local_file), "/Docs/a.txt")

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error
    assert "'/Docs/a.txt'" in str(raised.value)


@pytest.mark.parametrize(
    ("error", "expected"),
    [(rate_limited(), ConnectionError), (server_error(), OSError)],
)
def test_replace_lookup_errors_become_the_documented_exceptions(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
    error: Exception,
    expected: type[Exception],
) -> None:
    """A failure reading the current revision is translated and nothing is uploaded."""
    dbx.files_get_metadata.side_effect = error

    with pytest.raises(expected) as raised:
        client.replace_file(str(local_file), "/Docs/a.txt")

    assert type(raised.value) is expected
    dbx.files_upload.assert_not_called()


def test_replace_missing_local_file_does_not_upload(
    client: DropboxClient,
    dbx: MagicMock,
    existing: files.FileMetadata,
) -> None:
    """A local path that does not exist raises FileNotFoundError before any upload."""
    assert existing.rev == REV
    with pytest.raises(FileNotFoundError):
        client.replace_file("/non/existent/local/path.txt", "/Docs/a.txt")

    dbx.files_upload.assert_not_called()
