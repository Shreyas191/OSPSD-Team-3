"""
Unit tests for DropboxClient read functions
- download_file (files_download_to_file)
- get_metadata (files_get_metadata)
- list_folder (files_list_folder and files_list_folder_continue pagination)
- search (files_search_v2 and files_search_continue_v2 pagination)
"""

from datetime import datetime, timezone
from unittest.mock import Mock, call

import pytest
from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata, FolderMetadata

from cloud_storage_client_api.file import File
from dropbox_client_impl.dropbox_impl import DropboxClient


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
    now = modified or datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    return FileMetadata(
        name=name,
        id=file_id,
        path_display=path,
        client_modified=now,
        server_modified=now,
        rev="rev12345",
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


def create_search_match(metadata: FileMetadata | FolderMetadata) -> Mock:
    """Helper creating a mock SearchMatchV2 pointing to real metadata via MetadataV2 union."""
    match = Mock()
    match.metadata.is_metadata.return_value = True
    match.metadata.get_metadata.return_value = metadata
    return match


# download_file tests

def test_download_file_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """download_file calls files_download_to_file with exact paths and returns File."""
    fake_meta = create_fake_file_metadata()
    mock_dbx.files_download_to_file.return_value = fake_meta

    result = client.download_file("/docs/report.pdf", "/local/path/report.pdf")

    mock_dbx.files_download_to_file.assert_called_once_with(
        download_path="/local/path/report.pdf",
        path="/docs/report.pdf",
    )
    assert isinstance(result, File)
    assert result.name == "report.pdf"
    assert result.path == "/docs/report.pdf"
    assert result.id == "id:abc123file"
    assert result.is_folder is False
    assert result.size == 2048
    assert result.modified == fake_meta.server_modified


def test_download_file_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """download_file propagates Dropbox ApiError when download fails."""
    mock_dbx.files_download_to_file.side_effect = ApiError(
        request_id="123",
        error=Mock(),
        user_message_text="File not found",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        client.download_file("/missing.txt", "/local/missing.txt")


# get_metadata tests

def test_get_metadata_file_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """get_metadata returns proper File model for a file."""
    fake_meta = create_fake_file_metadata(name="data.csv", path="/data.csv", size=512)
    mock_dbx.files_get_metadata.return_value = fake_meta

    result = client.get_metadata("/data.csv")

    mock_dbx.files_get_metadata.assert_called_once_with("/data.csv")
    assert isinstance(result, File)
    assert result.name == "data.csv"
    assert result.path == "/data.csv"
    assert result.id == "id:abc123file"
    assert result.is_folder is False
    assert result.size == 512
    assert result.modified == fake_meta.server_modified


def test_get_metadata_folder_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """get_metadata returns proper File model for a folder (size and modified are None)."""
    fake_meta = create_fake_folder_metadata(name="photos", path="/photos", folder_id="id:folder999")
    mock_dbx.files_get_metadata.return_value = fake_meta

    result = client.get_metadata("/photos")

    mock_dbx.files_get_metadata.assert_called_once_with("/photos")
    assert isinstance(result, File)
    assert result.name == "photos"
    assert result.path == "/photos"
    assert result.id == "id:folder999"
    assert result.is_folder is True
    assert result.size is None
    assert result.modified is None


def test_get_metadata_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """get_metadata propagates Dropbox ApiError on non-existent path."""
    mock_dbx.files_get_metadata.side_effect = ApiError(
        request_id="456",
        error=Mock(),
        user_message_text="Path not found",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        client.get_metadata("/invalid/path")


# list_folder tests

def test_list_folder_single_page(client: DropboxClient, mock_dbx: Mock) -> None:
    """list_folder lists entries without pagination when has_more is False."""
    entry_file = create_fake_file_metadata("file1.txt", "/test/file1.txt")
    entry_folder = create_fake_folder_metadata("subfolder", "/test/subfolder")

    page = Mock(entries=[entry_file, entry_folder], has_more=False)
    mock_dbx.files_list_folder.return_value = page

    results = list(client.list_folder("/test"))

    mock_dbx.files_list_folder.assert_called_once_with("/test")
    mock_dbx.files_list_folder_continue.assert_not_called()
    assert len(results) == 2
    assert all(isinstance(r, File) for r in results)
    assert results[0].is_folder is False
    assert results[1].is_folder is True


def test_list_folder_multi_page_pagination(client: DropboxClient, mock_dbx: Mock) -> None:
    """list_folder loops across 3 pages until has_more is False and stops."""
    entry1 = create_fake_file_metadata("file1.txt", "/paginated/file1.txt")
    entry2 = create_fake_file_metadata("file2.txt", "/paginated/file2.txt")
    entry3 = create_fake_file_metadata("file3.txt", "/paginated/file3.txt")

    page1 = Mock(entries=[entry1], has_more=True, cursor="cur1")
    page2 = Mock(entries=[entry2], has_more=True, cursor="cur2")
    page3 = Mock(entries=[entry3], has_more=False, cursor="cur3")

    mock_dbx.files_list_folder.return_value = page1
    mock_dbx.files_list_folder_continue.side_effect = [page2, page3]

    results = list(client.list_folder("/paginated"))

    mock_dbx.files_list_folder.assert_called_once_with("/paginated")
    assert mock_dbx.files_list_folder_continue.call_args_list == [
        call("cur1"),
        call("cur2"),
    ]
    assert len(results) == 3


def test_list_folder_root(client: DropboxClient, mock_dbx: Mock) -> None:
    """list_folder handles default/empty root path correctly."""
    page = Mock(entries=[], has_more=False)
    mock_dbx.files_list_folder.return_value = page

    results = list(client.list_folder())

    mock_dbx.files_list_folder.assert_called_once_with("")
    assert results == []


def test_list_folder_initial_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """list_folder propagates Dropbox ApiError when initial call fails."""
    mock_dbx.files_list_folder.side_effect = ApiError(
        request_id="789",
        error=Mock(),
        user_message_text="Path not found",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        list(client.list_folder("/nonexistent"))


def test_list_folder_continuation_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """list_folder propagates Dropbox ApiError when pagination continuation fails."""
    entry1 = create_fake_file_metadata("file1.txt", "/paginated/file1.txt")
    page1 = Mock(entries=[entry1], has_more=True, cursor="cur1")

    mock_dbx.files_list_folder.return_value = page1
    mock_dbx.files_list_folder_continue.side_effect = ApiError(
        request_id="790",
        error=Mock(),
        user_message_text="Continuation failed",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        list(client.list_folder("/paginated"))


# search tests

def test_search_file_success_and_options(client: DropboxClient, mock_dbx: Mock) -> None:
    """search invokes files_search_v2 with query, max_results, and filename_only."""
    file_meta = create_fake_file_metadata("match.txt", "/match.txt")
    match = create_search_match(file_meta)

    search_result = Mock(matches=[match], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("match", max_results=10))

    mock_dbx.files_search_v2.assert_called_once()
    call_args, call_kwargs = mock_dbx.files_search_v2.call_args

    query = call_kwargs.get("query") if "query" in call_kwargs else call_args[0]
    assert query == "match"

    options = call_kwargs.get("options") if "options" in call_kwargs else (call_args[1] if len(call_args) > 1 else None)
    assert options is not None, "SearchOptions must be passed to files_search_v2"
    assert options.max_results == 10
    assert options.filename_only is True

    mock_dbx.files_search_continue_v2.assert_not_called()
    assert len(results) == 1
    assert isinstance(results[0], File)
    assert results[0].name == "match.txt"
    assert results[0].is_folder is False


def test_search_folder_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """search yields matched folders with correct folder contract attributes."""
    folder_meta = create_fake_folder_metadata("matched_folder", "/matched_folder")
    match = create_search_match(folder_meta)

    search_result = Mock(matches=[match], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("matched_folder"))

    assert len(results) == 1
    assert isinstance(results[0], File)
    assert results[0].name == "matched_folder"
    assert results[0].is_folder is True
    assert results[0].size is None
    assert results[0].modified is None


def test_search_empty_matches(client: DropboxClient, mock_dbx: Mock) -> None:
    """search returns an empty iterator when no matches are found."""
    search_result = Mock(matches=[], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("nonexistent", max_results=5))

    mock_dbx.files_search_v2.assert_called_once()
    mock_dbx.files_search_continue_v2.assert_not_called()
    assert results == []


def test_search_skips_non_metadata_matches(client: DropboxClient, mock_dbx: Mock) -> None:
    """search ignores matches where metadata is not available (is_metadata is False)."""
    match_deleted = Mock()
    match_deleted.metadata.is_metadata.return_value = False

    search_result = Mock(matches=[match_deleted], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("deleted_file"))

    assert results == []
    match_deleted.metadata.get_metadata.assert_not_called()


def test_search_multi_page_pagination(client: DropboxClient, mock_dbx: Mock) -> None:
    """search loops across 3 pages using files_search_continue_v2 until has_more is False."""
    m1 = create_search_match(create_fake_file_metadata("res1.txt", "/res1.txt"))
    m2 = create_search_match(create_fake_file_metadata("res2.txt", "/res2.txt"))
    m3 = create_search_match(create_fake_file_metadata("res3.txt", "/res3.txt"))

    page1 = Mock(matches=[m1], has_more=True, cursor="cur1")
    page2 = Mock(matches=[m2], has_more=True, cursor="cur2")
    page3 = Mock(matches=[m3], has_more=False, cursor="cur3")

    mock_dbx.files_search_v2.return_value = page1
    mock_dbx.files_search_continue_v2.side_effect = [page2, page3]

    results = list(client.search("res", max_results=10))

    mock_dbx.files_search_v2.assert_called_once()
    assert mock_dbx.files_search_continue_v2.call_args_list == [
        call("cur1"),
        call("cur2"),
    ]
    assert len(results) == 3


def test_search_respects_max_results_limit_on_yield(client: DropboxClient, mock_dbx: Mock) -> None:
    """search yields at most max_results items even if additional matches exist."""
    m1 = create_search_match(create_fake_file_metadata("res1.txt", "/res1.txt"))
    m2 = create_search_match(create_fake_file_metadata("res2.txt", "/res2.txt"))
    m3 = create_search_match(create_fake_file_metadata("res3.txt", "/res3.txt"))

    page1 = Mock(matches=[m1, m2, m3], has_more=True, cursor="cur1")
    mock_dbx.files_search_v2.return_value = page1

    results = list(client.search("res", max_results=2))

    assert len(results) == 2
    mock_dbx.files_search_continue_v2.assert_not_called()


def test_search_initial_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """search propagates Dropbox ApiError when initial search call fails."""
    mock_dbx.files_search_v2.side_effect = ApiError(
        request_id="999",
        error=Mock(),
        user_message_text="Search failed",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        list(client.search("fail"))


def test_search_continuation_api_error_propagates(client: DropboxClient, mock_dbx: Mock) -> None:
    """search propagates Dropbox ApiError when search pagination continuation fails."""
    m1 = create_search_match(create_fake_file_metadata("res1.txt", "/res1.txt"))
    page1 = Mock(matches=[m1], has_more=True, cursor="cur1")

    mock_dbx.files_search_v2.return_value = page1
    mock_dbx.files_search_continue_v2.side_effect = ApiError(
        request_id="1000",
        error=Mock(),
        user_message_text="Search continue failed",
        user_message_locale="en",
    )

    with pytest.raises(ApiError):
        list(client.search("res"))