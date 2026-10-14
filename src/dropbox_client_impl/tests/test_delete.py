"""Unit tests for DropboxClient.delete.

The Dropbox SDK is mocked, so these tests need no network or credentials.
"""

import re
from unittest.mock import Mock

import pytest
from dropbox import exceptions, files

from dropbox_client_impl import DropboxClient


def _api_error(error: object) -> exceptions.ApiError:
    return exceptions.ApiError("req-1", error, None, None)


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
    dbx.files_delete_v2.side_effect = _api_error(
        files.DeleteError.path_lookup(files.LookupError.not_found),
    )

    with pytest.raises(FileNotFoundError, match=re.escape("/Docs/missing.pdf")):
        DropboxClient(dbx=dbx).delete("/Docs/missing.pdf")


@pytest.mark.parametrize(
    "error",
    [
        files.DeleteError.path_lookup(files.LookupError.restricted_content),
        files.DeleteError.too_many_write_operations,
        "unexpected",
    ],
)
def test_other_dropbox_errors_propagate(dbx: Mock, error: object) -> None:
    """Errors with no domain meaning are re-raised unchanged, not disguised as missing files."""
    api_error = _api_error(error)
    dbx.files_delete_v2.side_effect = api_error

    with pytest.raises(exceptions.ApiError) as raised:
        DropboxClient(dbx=dbx).delete("/Docs/report.pdf")

    assert raised.value is api_error
