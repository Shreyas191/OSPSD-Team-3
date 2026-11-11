"""Unit tests for ``DropboxClient.upload_file``.

The Dropbox SDK is an autospec mock (see ``conftest.py``) that returns and raises real SDK
objects, so each test checks the exact ``files_upload`` call, the ``File`` built from the
metadata Dropbox returns, and the documented exception each Dropbox failure becomes.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from dropbox import auth, exceptions, files, stone_validators
from dropbox.files import WriteMode
from tests.dropbox_sdk import api_error, file_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient


def _write_failed(write: files.WriteError) -> exceptions.ApiError:
    """Return the error Dropbox raises when writing the uploaded file fails with ``write``."""
    return api_error(
        files.UploadError.path(files.UploadWriteFailed(reason=write, upload_session_id="s")),
    )


def _conflict() -> exceptions.ApiError:
    return _write_failed(files.WriteError.conflict(files.WriteConflictError.file))


@pytest.fixture
def local_file(tmp_path: Path) -> Path:
    """Return a small local file to upload."""
    path = tmp_path / "a.txt"
    path.write_bytes(b"data")
    return path


@pytest.mark.parametrize(
    ("overwrite", "mode"),
    [(False, WriteMode.add), (True, WriteMode.overwrite)],
)
def test_upload_sends_file_bytes_and_returns_the_file(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
    *,
    overwrite: bool,
    mode: WriteMode,
) -> None:
    """Upload sends the bytes with the write mode for ``overwrite`` and returns the new file."""
    metadata = file_metadata("/Docs/a.txt", size=4)
    dbx.files_upload.return_value = metadata

    uploaded = client.upload_file(str(local_file), "/Docs/a.txt", overwrite=overwrite)

    dbx.files_upload.assert_called_once_with(
        b"data",
        "/Docs/a.txt",
        mode=mode,
        autorename=False,
        strict_conflict=not overwrite,
    )
    assert uploaded.path == "/Docs/a.txt"
    assert uploaded.name == "a.txt"
    assert uploaded.id == "id:file123"
    assert uploaded.is_folder is False
    assert uploaded.size == 4
    assert uploaded.modified == metadata.server_modified


def test_upload_empty_file(client: DropboxClient, dbx: MagicMock, tmp_path: Path) -> None:
    """An empty local file is uploaded as zero bytes."""
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    dbx.files_upload.return_value = file_metadata("/empty.txt", size=0)

    uploaded = client.upload_file(str(empty), "/empty.txt")

    dbx.files_upload.assert_called_once_with(
        b"",
        "/empty.txt",
        mode=WriteMode.add,
        autorename=False,
        strict_conflict=True,
    )
    assert uploaded.size == 0


def test_upload_missing_local_file_does_not_call_dropbox(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """A local path that does not exist raises FileNotFoundError before contacting Dropbox."""
    with pytest.raises(FileNotFoundError):
        client.upload_file("/non/existent/local/path.txt", "/remote.txt")

    dbx.files_upload.assert_not_called()


def test_upload_identical_content_without_overwrite_is_a_conflict(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
) -> None:
    """With strict_conflict, Dropbox reports a conflict even for identical bytes."""
    dbx.files_upload.side_effect = _conflict()

    with pytest.raises(FileExistsError, match=r"Something already exists at '/Docs/a\.txt'"):
        client.upload_file(str(local_file), "/Docs/a.txt", overwrite=False)

    dbx.files_upload.assert_called_once_with(
        b"data",
        "/Docs/a.txt",
        mode=WriteMode.add,
        autorename=False,
        strict_conflict=True,
    )


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
        # Upload-specific reasons.
        (_conflict(), FileExistsError),
        (_write_failed(files.WriteError.malformed_path("bad")), ValueError),
        (_write_failed(files.WriteError.disallowed_name), ValueError),
        (api_error(files.UploadError.payload_too_large), ValueError),
        (_write_failed(files.WriteError.no_write_permission), PermissionError),
        (_write_failed(files.WriteError.team_folder), PermissionError),
        (_write_failed(files.WriteError.too_many_write_operations), ConnectionError),
        (_write_failed(files.WriteError.insufficient_space), OSError),
        (api_error(files.UploadError.content_hash_mismatch), OSError),
    ],
)
def test_upload_errors_become_the_documented_exceptions(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
    error: Exception,
    expected: type[Exception],
) -> None:
    """No Dropbox exception escapes an upload: each failure becomes one documented built-in."""
    dbx.files_upload.side_effect = error

    with pytest.raises(expected) as raised:
        client.upload_file(str(local_file), "/Docs/report.txt")

    assert type(raised.value) is expected
    assert raised.value.__cause__ is error
    assert "'/Docs/report.txt'" in str(raised.value)


def test_upload_too_large_message_explains_the_limit(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
) -> None:
    """A file over Dropbox's single-upload limit is reported as invalid input with the limit."""
    dbx.files_upload.side_effect = api_error(files.UploadError.payload_too_large)

    with pytest.raises(ValueError, match="150 MB"):
        client.upload_file(str(local_file), "/big.bin")


def test_upload_provider_failure_message_names_the_dropbox_error(
    client: DropboxClient,
    dbx: MagicMock,
    local_file: Path,
) -> None:
    """A provider failure's message keeps the Dropbox error tag for the service's logs."""
    dbx.files_upload.side_effect = _write_failed(files.WriteError.insufficient_space)

    with pytest.raises(
        OSError,
        match=r"Dropbox failed uploading to '/big.bin'.*insufficient_space",
    ):
        client.upload_file(str(local_file), "/big.bin")
