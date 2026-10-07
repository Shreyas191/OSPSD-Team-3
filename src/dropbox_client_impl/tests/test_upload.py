"""Tests for Dropbox client upload functionality."""
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import dropbox
import pytest
from cloud_storage_client_api.file import File
from dropbox.exceptions import ApiError
from dropbox.files import (
    CreateFolderResult,
    FileMetadata,
    FolderMetadata,
    RelocationResult,
    WriteMode,
)

from dropbox_client_impl.dropbox_impl import DropboxClient

# Jing: if we ever decide to make ruff ignore magic numbers in tests get rid of this
TWO = 2
FILE_SIZE = 4096

"""TODO(Jing): merge these stubs with test_reads' implementations"""
# fixtures

@pytest.fixture
def mock_dbx() -> Mock:
    """Fixture providing a mocked Dropbox SDK instance."""
    return Mock()


@pytest.fixture
def client(mock_dbx: Mock) -> DropboxClient:
    """Fixture providing a DropboxClient configured with the mocked Dropbox instance."""
    return DropboxClient(dbx=mock_dbx)


def create_fake_file_metadata(
    name: str = "report.pdf",
    path: str = "/docs/report.pdf",
    file_id: str = "id:abc123file",
    size: int = 2048,
    modified: datetime | None = None,
) -> FileMetadata:
    """Create a real Dropbox SDK FileMetadata instance for isinstance checks."""
    now = modified or datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    return FileMetadata(
        name=name,
        id=file_id,
        path_display=path,
        client_modified=now,
        server_modified=now,
        rev="123456789",
        size=size,
    )


def create_fake_folder_metadata(
    name: str = "photos",
    path: str = "/photos",
    folder_id: str = "id:xyz789folder",
) -> FolderMetadata:
    """Create a real Dropbox SDK FolderMetadata instance for isinstance checks."""
    return FolderMetadata(
        name=name,
        id=folder_id,
        path_display=path,
    )

# below stubs don't with test_reads

def create_fake_create_folder_result(folder_metadata: FolderMetadata) -> CreateFolderResult:
    """Create a real Dropbox SDK CreateFolderResult containing FolderMetadata."""
    return CreateFolderResult(metadata=folder_metadata)


def create_fake_relocation_result(
    metadata: FileMetadata | FolderMetadata,
) -> RelocationResult:
    """Create a real Dropbox SDK RelocationResult containing FileMetadata or FolderMetadata."""
    return RelocationResult(metadata=metadata)

# end fixtures


# upload_file tests

def test_client_upload_file(tmp_path: Path) -> None:
    """Upload a local file and return its metadata. This test uses a mock Dropbox client to avoid actual network calls."""
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"sample PDF bytes")

    mock_dbx = cast("dropbox.Dropbox", Mock(spec=dropbox.Dropbox))
    mock_dbx.files_upload.return_value = SimpleNamespace(
        path_display="/Docs/report.pdf",
        path_lower="/docs/report.pdf",
    )
    client = DropboxClient(dbx=mock_dbx)

    uploaded = client.upload_file(
        local_path=str(local_file),
        remote_path="/Docs/report.pdf",
    )

    assert uploaded.path == "/Docs/report.pdf"
    mock_dbx.files_upload.assert_called_once()

def test_upload_file_default_mode_success(
    client: DropboxClient,
    mock_dbx: Mock,
    tmp_path: Path,
) -> None:
    """Upload_file reads local file bytes, passes WriteMode.add by default, and returns File."""
    local_file = tmp_path / "notes.txt"
    content = b"hello dropbox"
    local_file.write_bytes(content)

    fake_meta = create_fake_file_metadata(
        name="notes.txt",
        path="/notes.txt",
        size=len(content),
    )
    mock_dbx.files_upload.return_value = fake_meta

    result = client.upload_file(str(local_file), "/notes.txt")

    mock_dbx.files_upload.assert_called_once()
    call_args, call_kwargs = mock_dbx.files_upload.call_args

    # check binary data and destination path
    f_data = call_kwargs.get("f") if "f" in call_kwargs else call_args[0]
    dest_path = call_kwargs.get("path") if "path" in call_kwargs else call_args[1]
    mode = call_kwargs.get("mode") if "mode" in call_kwargs else (call_args[2] if len(call_args) > TWO else None)
    autorename = call_kwargs.get("autorename") if "autorename" in call_kwargs else False

    assert f_data == content
    assert dest_path == "/notes.txt"
    assert mode == WriteMode.add
    assert autorename is False

    assert isinstance(result, File)
    assert result.name == "notes.txt"
    assert result.path == "/notes.txt"
    assert result.is_folder is False
    assert result.size == len(content)
    assert result.modified == fake_meta.server_modified


def test_upload_file_overwrite_mode_success(
    client: DropboxClient,
    mock_dbx: Mock,
    tmp_path: Path,
) -> None:
    """Upload_file passes WriteMode.overwrite when overwrite=True."""
    local_file = tmp_path / "data.csv"
    local_file.write_bytes(b"a,b,c\n1,2,3")

    fake_meta = create_fake_file_metadata(name="data.csv", path="/data.csv", size=11)
    mock_dbx.files_upload.return_value = fake_meta

    result = client.upload_file(str(local_file), "/data.csv", overwrite=True)

    mock_dbx.files_upload.assert_called_once()
    _, call_kwargs = mock_dbx.files_upload.call_args
    assert call_kwargs.get("mode") == WriteMode.overwrite
    assert isinstance(result, File)
    assert result.path == "/data.csv"


def test_upload_file_empty_file_success(
    client: DropboxClient,
    mock_dbx: Mock,
    tmp_path: Path,
) -> None:
    """Upload_file handles empty files (0 bytes) properly."""
    empty_file = tmp_path / "empty.txt"
    empty_file.write_bytes(b"")

    fake_meta = create_fake_file_metadata(name="empty.txt", path="/empty.txt", size=0)
    mock_dbx.files_upload.return_value = fake_meta

    result = client.upload_file(str(empty_file), "/empty.txt")

    mock_dbx.files_upload.assert_called_once()
    call_args, call_kwargs = mock_dbx.files_upload.call_args
    f_data = call_kwargs.get("f") if "f" in call_kwargs else call_args[0]
    assert f_data == b""
    assert result.size == 0


def test_upload_file_api_error_propagates(
    client: DropboxClient,
    mock_dbx: Mock,
    tmp_path: Path,
) -> None:
    """Upload_file propagates Dropbox ApiError when files_upload fails on server."""
    local_file = tmp_path / "upload_fail.txt"
    local_file.write_bytes(b"data")

    mock_dbx.files_upload.side_effect = ApiError(
        request_id="up123",
        error=Mock(),
        user_message_text="Conflict: file already exists",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        client.upload_file(str(local_file), "/upload_fail.txt")


def test_upload_file_missing_local_path_raises_file_not_found(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Upload_file raises FileNotFoundError if local path does not exist without calling API."""
    with pytest.raises(FileNotFoundError):
        client.upload_file("/non/existent/local/path.txt", "/remote.txt")

    mock_dbx.files_upload.assert_not_called()


# create_folder tests

def test_create_folder_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """Create_folder calls files_create_folder_v2 and returns File with folder contract."""
    folder_meta = create_fake_folder_metadata("documents", "/documents", "id:folder101")
    folder_result = create_fake_create_folder_result(folder_meta)
    mock_dbx.files_create_folder_v2.return_value = folder_result

    result = client.create_folder("/documents")

    mock_dbx.files_create_folder_v2.assert_called_once()
    call_args, call_kwargs = mock_dbx.files_create_folder_v2.call_args
    passed_path = call_kwargs.get("path") if "path" in call_kwargs else call_args[0]
    assert passed_path == "/documents"

    assert isinstance(result, File)
    assert result.name == "documents"
    assert result.path == "/documents"
    assert result.id == "id:folder101"
    assert result.is_folder is True
    assert result.size is None
    assert result.modified is None


def test_create_folder_nested_path_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """Create_folder handles deeply nested folder paths properly."""
    nested_path = "/parent/subfolder/target"
    folder_meta = create_fake_folder_metadata("target", nested_path, "id:nested_target")
    folder_result = create_fake_create_folder_result(folder_meta)
    mock_dbx.files_create_folder_v2.return_value = folder_result

    result = client.create_folder(nested_path)

    mock_dbx.files_create_folder_v2.assert_called_once()
    call_args, call_kwargs = mock_dbx.files_create_folder_v2.call_args
    passed_path = call_kwargs.get("path") if "path" in call_kwargs else call_args[0]
    assert passed_path == nested_path

    assert isinstance(result, File)
    assert result.name == "target"
    assert result.path == nested_path
    assert result.is_folder is True


def test_create_folder_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """Create_folder propagates Dropbox ApiError when folder creation fails."""
    mock_dbx.files_create_folder_v2.side_effect = ApiError(
        request_id="cf456",
        error=Mock(),
        user_message_text="Folder already exists",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        client.create_folder("/already_existing_folder")


# copy_file tests

def test_copy_file_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """Copy_file calls files_copy_v2 with from/to paths and returns destination File."""
    copied_meta = create_fake_file_metadata(
        name="report_copy.pdf",
        path="/backup/report_copy.pdf",
        file_id="id:copy123file",
        size=FILE_SIZE,
    )
    relocation_result = create_fake_relocation_result(copied_meta)
    mock_dbx.files_copy_v2.return_value = relocation_result

    result = client.copy_file("/docs/report.pdf", "/backup/report_copy.pdf")

    mock_dbx.files_copy_v2.assert_called_once()
    call_args, call_kwargs = mock_dbx.files_copy_v2.call_args
    from_p = call_kwargs.get("from_path") if "from_path" in call_kwargs else call_args[0]
    to_p = call_kwargs.get("to_path") if "to_path" in call_kwargs else call_args[1]

    assert from_p == "/docs/report.pdf"
    assert to_p == "/backup/report_copy.pdf"

    assert isinstance(result, File)
    assert result.name == "report_copy.pdf"
    assert result.path == "/backup/report_copy.pdf"
    assert result.id == "id:copy123file"
    assert result.is_folder is False
    assert result.size == FILE_SIZE
    assert result.modified == copied_meta.server_modified


def test_copy_file_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """Copy_file propagates Dropbox ApiError when source file not found or conflict occurs."""
    mock_dbx.files_copy_v2.side_effect = ApiError(
        request_id="cp789",
        error=Mock(),
        user_message_text="Source file not found",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        client.copy_file("/missing.txt", "/backup/missing.txt")


# copy_folder tests

def test_copy_folder_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """Copy_folder calls files_copy_v2 with from/to paths and returns destination Folder."""
    copied_folder_meta = create_fake_folder_metadata(
        name="project_backup",
        path="/archives/project_backup",
        folder_id="id:folder_copy_789",
    )
    relocation_result = create_fake_relocation_result(copied_folder_meta)
    mock_dbx.files_copy_v2.return_value = relocation_result

    result = client.copy_folder("/active/project", "/archives/project_backup")

    mock_dbx.files_copy_v2.assert_called_once()
    call_args, call_kwargs = mock_dbx.files_copy_v2.call_args
    from_p = call_kwargs.get("from_path") if "from_path" in call_kwargs else call_args[0]
    to_p = call_kwargs.get("to_path") if "to_path" in call_kwargs else call_args[1]

    assert from_p == "/active/project"
    assert to_p == "/archives/project_backup"

    assert isinstance(result, File)
    assert result.name == "project_backup"
    assert result.path == "/archives/project_backup"
    assert result.id == "id:folder_copy_789"
    assert result.is_folder is True
    assert result.size is None
    assert result.modified is None


def test_copy_folder_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """Copy_folder propagates Dropbox ApiError when folder copying fails."""
    mock_dbx.files_copy_v2.side_effect = ApiError(
        request_id="cp999",
        error=Mock(),
        user_message_text="Cannot copy folder into itself",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        client.copy_folder("/work", "/work/nested")
