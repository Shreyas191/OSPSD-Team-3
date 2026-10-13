"""Unit tests for DropboxFile, using real Dropbox SDK metadata objects."""

from datetime import datetime

import pytest
from cloud_storage_client_api import File
from dropbox import files

from dropbox_client_impl import DropboxFile

SIZE = 2048
MODIFIED = datetime(2026, 10, 1, 12, 30)  # noqa: DTZ001 - Dropbox returns naive UTC datetimes


def _file_metadata() -> files.FileMetadata:
    return files.FileMetadata(
        name="report.pdf",
        id="id:abc123",
        client_modified=MODIFIED,
        server_modified=MODIFIED,
        rev="0123456789abcdef",
        size=SIZE,
        path_lower="/docs/report.pdf",
        path_display="/Docs/report.pdf",
    )


def _folder_metadata() -> files.FolderMetadata:
    return files.FolderMetadata(
        name="Docs",
        id="id:folder1",
        path_lower="/docs",
        path_display="/Docs",
    )


def test_dropbox_file_is_a_file() -> None:
    """DropboxFile satisfies the File contract."""
    assert isinstance(DropboxFile(_file_metadata()), File)


def test_file_metadata_properties() -> None:
    """File metadata maps onto every File property."""
    report = DropboxFile(_file_metadata())

    assert report.id == "id:abc123"
    assert report.name == "report.pdf"
    assert report.path == "/Docs/report.pdf"
    assert report.is_folder is False
    assert report.size == SIZE
    assert report.modified == MODIFIED


def test_folder_metadata_properties() -> None:
    """Folders have no size or modified time."""
    folder = DropboxFile(_folder_metadata())

    assert folder.id == "id:folder1"
    assert folder.name == "Docs"
    assert folder.path == "/Docs"
    assert folder.is_folder is True
    assert folder.size is None
    assert folder.modified is None


def test_modified_is_the_server_time_not_the_client_time() -> None:
    """``modified`` is when Dropbox stored the file, not the uploader's local timestamp."""
    metadata = _file_metadata()
    metadata.client_modified = datetime(2020, 1, 1)  # noqa: DTZ001

    assert DropboxFile(metadata).modified == MODIFIED


def test_deleted_metadata_is_rejected() -> None:
    """Deleted entries have no ID, so wrapping one fails loudly instead of returning ``""``."""
    deleted = files.DeletedMetadata(
        name="report.pdf",
        path_lower="/docs/report.pdf",
        path_display="/Docs/report.pdf",
    )

    with pytest.raises(TypeError, match="DeletedMetadata"):
        DropboxFile(deleted)


def test_repr() -> None:
    """The repr shows the kind and path."""
    assert repr(DropboxFile(_folder_metadata())) == "DropboxFile(folder, path='/Docs')"
    assert repr(DropboxFile(_file_metadata())) == "DropboxFile(file, path='/Docs/report.pdf')"
