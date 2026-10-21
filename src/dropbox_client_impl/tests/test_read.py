"""Unit tests for DropboxClient read functions.

- download_file (files_download_to_file)
- get_metadata (files_get_metadata)
- list_folder (files_list_folder and files_list_folder_continue pagination)
- search (files_search_v2 and files_search_continue_v2 pagination)
"""

from datetime import UTC, datetime
from unittest.mock import Mock, call

import dropbox
import pytest
import requests
from cloud_storage_client_api.file import File
from dropbox import exceptions, files, stone_validators
from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata, FolderMetadata

from dropbox_client_impl.dropbox_impl import DropboxClient

DEFAULT_FILE_SIZE_2048 = 2048
CUSTOM_FILE_SIZE_512 = 512

SEARCH_MAX_RESULTS_TWO = 2
SEARCH_MAX_RESULTS_FIVE = 5
SEARCH_MAX_RESULTS_TEN = 10

EXPECTED_COUNT_ONE = 1
EXPECTED_COUNT_TWO = 2
EXPECTED_COUNT_THREE = 3


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
    size: int = DEFAULT_FILE_SIZE_2048,
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


def create_search_match(metadata: FileMetadata | FolderMetadata) -> Mock:
    """Create a mock SearchMatchV2 pointing to real metadata via MetadataV2 union."""
    match = Mock()
    match.metadata.is_metadata.return_value = True
    match.metadata.get_metadata.return_value = metadata
    return match


# download_file tests


def test_download_file_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """Download_file calls files_download_to_file with exact paths and returns File."""
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
    assert result.size == DEFAULT_FILE_SIZE_2048
    assert result.modified == fake_meta.server_modified


def test_download_file_not_found_raises_file_not_found(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Download_file translates not_found lookup error to FileNotFoundError."""
    error = files.DownloadError.path(files.LookupError.not_found)
    mock_dbx.files_download_to_file.side_effect = ApiError("123", error, "File not found", "en")

    with pytest.raises(FileNotFoundError, match=r"No file at '/missing.txt'"):
        client.download_file("/missing.txt", "/local/missing.txt")


def test_download_file_folder_raises_is_a_directory_error(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Download_file translates not_file lookup error to IsADirectoryError."""
    error = files.DownloadError.path(files.LookupError.not_file)
    mock_dbx.files_download_to_file.side_effect = ApiError("123", error, "Path is folder", "en")

    with pytest.raises(IsADirectoryError, match="Path is a folder: '/folder'"):
        client.download_file("/folder", "/local/folder")


def test_download_file_api_error_becomes_oserror(client: DropboxClient, mock_dbx: Mock) -> None:
    """Download_file maps unmapped Dropbox ApiError to OSError."""
    mock_dbx.files_download_to_file.side_effect = ApiError(
        request_id="123",
        error=Mock(),
        user_message_text="Download failed",
        user_message_locale="en",
    )

    with pytest.raises(OSError, match=r"Dropbox failed reading '/missing.txt'"):
        client.download_file("/missing.txt", "/local/missing.txt")


# get_metadata tests


def test_get_metadata_file_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """Get_metadata returns proper File model for a file."""
    fake_meta = create_fake_file_metadata(
        name="data.csv",
        path="/data.csv",
        size=CUSTOM_FILE_SIZE_512,
    )
    mock_dbx.files_get_metadata.return_value = fake_meta

    result = client.get_metadata("/data.csv")

    mock_dbx.files_get_metadata.assert_called_once_with("/data.csv")
    assert isinstance(result, File)
    assert result.name == "data.csv"
    assert result.path == "/data.csv"
    assert result.id == "id:abc123file"
    assert result.is_folder is False
    assert result.size == CUSTOM_FILE_SIZE_512
    assert result.modified == fake_meta.server_modified


def test_get_metadata_folder_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """Get_metadata returns proper File model for a folder (size and modified are None)."""
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


def test_get_metadata_not_found_raises_file_not_found(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Get_metadata translates not_found lookup error to FileNotFoundError."""
    error = files.GetMetadataError.path(files.LookupError.not_found)
    mock_dbx.files_get_metadata.side_effect = ApiError("456", error, "Path not found", "en")

    with pytest.raises(FileNotFoundError, match="No file or folder at '/invalid/path'"):
        client.get_metadata("/invalid/path")


def test_get_metadata_api_error_becomes_oserror(client: DropboxClient, mock_dbx: Mock) -> None:
    """Get_metadata maps unmapped Dropbox ApiError to OSError."""
    mock_dbx.files_get_metadata.side_effect = ApiError(
        request_id="456",
        error=Mock(),
        user_message_text="Path not found",
        user_message_locale="en",
    )

    with pytest.raises(OSError, match="Dropbox failed reading '/invalid/path'"):
        client.get_metadata("/invalid/path")


# list_folder tests


def test_list_folder_pagination_boundary_zero_entries(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Boundary n = 0: folder has 0 entries, has_more=False yields 0 items."""
    page = Mock(entries=[], has_more=False)
    mock_dbx.files_list_folder.return_value = page

    results = list(client.list_folder("/empty"))

    mock_dbx.files_list_folder.assert_called_once_with("/empty")
    mock_dbx.files_list_folder_continue.assert_not_called()
    assert results == []


def test_list_folder_pagination_boundary_less_than_page_size(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Boundary n < page size: e.g. 2 entries when page size is larger, has_more=False."""
    entry1 = create_fake_file_metadata("file1.txt", "/test/file1.txt")
    entry2 = create_fake_file_metadata("file2.txt", "/test/file2.txt")
    page = Mock(entries=[entry1, entry2], has_more=False)
    mock_dbx.files_list_folder.return_value = page

    results = list(client.list_folder("/test"))

    mock_dbx.files_list_folder.assert_called_once_with("/test")
    mock_dbx.files_list_folder_continue.assert_not_called()
    assert [r.path for r in results] == ["/test/file1.txt", "/test/file2.txt"]


def test_list_folder_pagination_boundary_exactly_page_size(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Boundary n = page size: page has n entries and has_more=True, followed by empty page."""
    entries = [create_fake_file_metadata(f"file{i}.txt", f"/test/file{i}.txt") for i in range(3)]
    page1 = Mock(entries=entries, has_more=True, cursor="cursor_exact")
    page2 = Mock(entries=[], has_more=False)
    mock_dbx.files_list_folder.return_value = page1
    mock_dbx.files_list_folder_continue.return_value = page2

    results = list(client.list_folder("/test"))

    mock_dbx.files_list_folder.assert_called_once_with("/test")
    mock_dbx.files_list_folder_continue.assert_called_once_with("cursor_exact")
    assert [r.path for r in results] == [
        "/test/file0.txt",
        "/test/file1.txt",
        "/test/file2.txt",
    ]


def test_list_folder_pagination_boundary_greater_than_page_size(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Boundary n > page size: multiple pages, each entry appears exactly once."""
    entry1 = create_fake_file_metadata("file1.txt", "/paginated/file1.txt")
    entry2 = create_fake_file_metadata("file2.txt", "/paginated/file2.txt")
    entry3 = create_fake_file_metadata("file3.txt", "/paginated/file3.txt")
    entry4 = create_fake_file_metadata("file4.txt", "/paginated/file4.txt")

    page1 = Mock(entries=[entry1, entry2], has_more=True, cursor="cur1")
    page2 = Mock(entries=[entry3, entry4], has_more=False, cursor="cur2")

    mock_dbx.files_list_folder.return_value = page1
    mock_dbx.files_list_folder_continue.return_value = page2

    results = list(client.list_folder("/paginated"))

    mock_dbx.files_list_folder.assert_called_once_with("/paginated")
    mock_dbx.files_list_folder_continue.assert_called_once_with("cur1")
    # Verify every entry appears exactly once
    paths = [r.path for r in results]
    assert paths == [
        "/paginated/file1.txt",
        "/paginated/file2.txt",
        "/paginated/file3.txt",
        "/paginated/file4.txt",
    ]
    assert len(paths) == len(set(paths))


def test_list_folder_root(client: DropboxClient, mock_dbx: Mock) -> None:
    """List_folder handles default/empty root path correctly."""
    page = Mock(entries=[], has_more=False)
    mock_dbx.files_list_folder.return_value = page

    results = list(client.list_folder())

    mock_dbx.files_list_folder.assert_called_once_with("")
    assert results == []


def test_list_folder_not_found_raises_file_not_found(client: DropboxClient, mock_dbx: Mock) -> None:
    """List_folder translates not_found lookup error to FileNotFoundError."""
    error = files.ListFolderError.path(files.LookupError.not_found)
    mock_dbx.files_list_folder.side_effect = ApiError("789", error, "Path not found", "en")

    with pytest.raises(FileNotFoundError, match="No folder at '/nonexistent'"):
        list(client.list_folder("/nonexistent"))


def test_list_folder_file_raises_value_error(client: DropboxClient, mock_dbx: Mock) -> None:
    """List_folder translates not_folder lookup error to ValueError."""
    error = files.ListFolderError.path(files.LookupError.not_folder)
    mock_dbx.files_list_folder.side_effect = ApiError("789", error, "Not folder", "en")

    with pytest.raises(ValueError, match=r"Path is a file: '/file.txt'"):
        list(client.list_folder("/file.txt"))


def test_list_folder_initial_api_error_becomes_oserror(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """List_folder maps unmapped Dropbox ApiError to OSError."""
    mock_dbx.files_list_folder.side_effect = ApiError(
        request_id="789",
        error=Mock(),
        user_message_text="Path not found",
        user_message_locale="en",
    )

    with pytest.raises(OSError, match="Dropbox failed reading '/nonexistent'"):
        list(client.list_folder("/nonexistent"))


def test_list_folder_continuation_not_found_raises_file_not_found(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """List_folder translates continuation not_found error to FileNotFoundError."""
    entry1 = create_fake_file_metadata("file1.txt", "/paginated/file1.txt")
    page1 = Mock(entries=[entry1], has_more=True, cursor="cur1")
    error = files.ListFolderContinueError.path(files.LookupError.not_found)

    mock_dbx.files_list_folder.return_value = page1
    mock_dbx.files_list_folder_continue.side_effect = ApiError("790", error, "Not found", "en")

    with pytest.raises(FileNotFoundError, match="No folder at '/paginated'"):
        list(client.list_folder("/paginated"))


def test_list_folder_continuation_api_error_becomes_oserror(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """List_folder maps unmapped Dropbox ApiError when pagination continuation fails to OSError."""
    entry1 = create_fake_file_metadata("file1.txt", "/paginated/file1.txt")
    page1 = Mock(entries=[entry1], has_more=True, cursor="cur1")

    mock_dbx.files_list_folder.return_value = page1
    mock_dbx.files_list_folder_continue.side_effect = ApiError(
        request_id="790",
        error=Mock(),
        user_message_text="Continuation failed",
        user_message_locale="en",
    )

    with pytest.raises(OSError, match="Dropbox failed reading '/paginated'"):
        list(client.list_folder("/paginated"))


def test_list_folder_continuation_reset_becomes_oserror(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """List_folder maps cursor reset to OSError."""
    entry1 = create_fake_file_metadata("file1.txt", "/paginated/file1.txt")
    page1 = Mock(entries=[entry1], has_more=True, cursor="cur1")

    mock_dbx.files_list_folder.return_value = page1
    mock_dbx.files_list_folder_continue.side_effect = ApiError(
        request_id="790",
        error=files.ListFolderContinueError.reset,
        user_message_text="Reset",
        user_message_locale="en",
    )

    with pytest.raises(OSError, match="Dropbox cursor was invalidated while listing '/paginated'"):
        list(client.list_folder("/paginated"))


# search tests


def test_search_file_success_and_options(client: DropboxClient, mock_dbx: Mock) -> None:
    """Search invokes files_search_v2 with query, max_results, and filename_only."""
    file_meta = create_fake_file_metadata("match.txt", "/match.txt")
    match = create_search_match(file_meta)

    search_result = Mock(matches=[match], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("match", max_results=SEARCH_MAX_RESULTS_TEN))

    mock_dbx.files_search_v2.assert_called_once()
    call_args, call_kwargs = mock_dbx.files_search_v2.call_args

    query = call_kwargs.get("query") if "query" in call_kwargs else call_args[0]
    assert query == "match"

    options = (
        call_kwargs.get("options")
        if "options" in call_kwargs
        else (call_args[1] if len(call_args) > 1 else None)
    )
    assert options is not None, "SearchOptions must be passed to files_search_v2"
    assert options.max_results == SEARCH_MAX_RESULTS_TEN
    assert options.filename_only is True

    mock_dbx.files_search_continue_v2.assert_not_called()
    assert len(results) == EXPECTED_COUNT_ONE
    assert isinstance(results[0], File)
    assert results[0].name == "match.txt"
    assert results[0].is_folder is False


def test_search_folder_success(client: DropboxClient, mock_dbx: Mock) -> None:
    """Search yields matched folders with correct folder contract attributes."""
    folder_meta = create_fake_folder_metadata("matched_folder", "/matched_folder")
    match = create_search_match(folder_meta)

    search_result = Mock(matches=[match], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("matched_folder"))

    assert len(results) == EXPECTED_COUNT_ONE
    assert isinstance(results[0], File)
    assert results[0].name == "matched_folder"
    assert results[0].is_folder is True
    assert results[0].size is None
    assert results[0].modified is None


def test_search_empty_matches(client: DropboxClient, mock_dbx: Mock) -> None:
    """Search returns an empty iterator when no matches are found."""
    search_result = Mock(matches=[], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("nonexistent", max_results=SEARCH_MAX_RESULTS_FIVE))

    mock_dbx.files_search_v2.assert_called_once()
    mock_dbx.files_search_continue_v2.assert_not_called()
    assert results == []


def test_search_skips_non_metadata_matches(client: DropboxClient, mock_dbx: Mock) -> None:
    """Search ignores matches where metadata is not available (is_metadata is False)."""
    match_deleted = Mock()
    match_deleted.metadata.is_metadata.return_value = False

    search_result = Mock(matches=[match_deleted], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("deleted_file"))

    assert results == []
    match_deleted.metadata.get_metadata.assert_not_called()


def test_search_multi_page_pagination(client: DropboxClient, mock_dbx: Mock) -> None:
    """Search loops across 3 pages using files_search_continue_v2 until has_more is False."""
    m1 = create_search_match(create_fake_file_metadata("res1.txt", "/res1.txt"))
    m2 = create_search_match(create_fake_file_metadata("res2.txt", "/res2.txt"))
    m3 = create_search_match(create_fake_file_metadata("res3.txt", "/res3.txt"))

    page1 = Mock(matches=[m1], has_more=True, cursor="cur1")
    page2 = Mock(matches=[m2], has_more=True, cursor="cur2")
    page3 = Mock(matches=[m3], has_more=False, cursor="cur3")

    mock_dbx.files_search_v2.return_value = page1
    mock_dbx.files_search_continue_v2.side_effect = [page2, page3]

    results = list(client.search("res", max_results=SEARCH_MAX_RESULTS_TEN))

    mock_dbx.files_search_v2.assert_called_once()
    assert mock_dbx.files_search_continue_v2.call_args_list == [
        call("cur1"),
        call("cur2"),
    ]
    assert len(results) == EXPECTED_COUNT_THREE


def test_search_respects_max_results_limit_on_yield(client: DropboxClient, mock_dbx: Mock) -> None:
    """Search yields at most max_results items even if additional matches exist."""
    m1 = create_search_match(create_fake_file_metadata("res1.txt", "/res1.txt"))
    m2 = create_search_match(create_fake_file_metadata("res2.txt", "/res2.txt"))
    m3 = create_search_match(create_fake_file_metadata("res3.txt", "/res3.txt"))

    page1 = Mock(matches=[m1, m2, m3], has_more=True, cursor="cur1")
    mock_dbx.files_search_v2.return_value = page1

    results = list(client.search("res", max_results=SEARCH_MAX_RESULTS_TWO))

    assert len(results) == EXPECTED_COUNT_TWO
    mock_dbx.files_search_continue_v2.assert_not_called()


def test_search_empty_query_raises_value_error(client: DropboxClient) -> None:
    """Search raises ValueError when query is empty."""
    with pytest.raises(ValueError, match="Search query must be non-empty"):
        list(client.search(""))


def test_search_invalid_max_results_raises_value_error(client: DropboxClient) -> None:
    """Search raises ValueError when max_results is not in [1, 1000]."""
    with pytest.raises(ValueError, match="max_results must be greater than 0"):
        list(client.search("query", max_results=0))
    with pytest.raises(ValueError, match="max_results must be greater than 0"):
        list(client.search("query", max_results=-1))
    with pytest.raises(ValueError, match="max_results must be greater than 0 and at most 1000"):
        list(client.search("query", max_results=1001))
    with pytest.raises(ValueError, match="max_results must be greater than 0 and at most 1000"):
        list(client.search("query", max_results=5000))


def test_search_skips_deleted_metadata_matches(client: DropboxClient, mock_dbx: Mock) -> None:
    """Search ignores matches where metadata is DeletedMetadata."""
    deleted_meta = files.DeletedMetadata(name="deleted.txt")
    match_deleted = Mock()
    match_deleted.metadata.is_metadata.return_value = True
    match_deleted.metadata.get_metadata.return_value = deleted_meta

    file_meta = create_fake_file_metadata("active.txt", "/active.txt")
    match_active = create_search_match(file_meta)

    search_result = Mock(matches=[match_deleted, match_active], has_more=False)
    mock_dbx.files_search_v2.return_value = search_result

    results = list(client.search("active"))
    assert len(results) == EXPECTED_COUNT_ONE
    assert results[0].name == "active.txt"


def test_search_initial_api_error_becomes_oserror(client: DropboxClient, mock_dbx: Mock) -> None:
    """Search maps unmapped Dropbox ApiError on initial search to OSError."""
    mock_dbx.files_search_v2.side_effect = ApiError(
        request_id="999",
        error=Mock(),
        user_message_text="Search failed",
        user_message_locale="en",
    )

    with pytest.raises(OSError, match="Dropbox search failed for 'fail'"):
        list(client.search("fail"))


def test_search_continuation_api_error_becomes_oserror(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Search maps unmapped Dropbox ApiError on continuation to OSError."""
    m1 = create_search_match(create_fake_file_metadata("res1.txt", "/res1.txt"))
    page1 = Mock(matches=[m1], has_more=True, cursor="cur1")

    mock_dbx.files_search_v2.return_value = page1
    mock_dbx.files_search_continue_v2.side_effect = ApiError(
        request_id="1000",
        error=Mock(),
        user_message_text="Search continue failed",
        user_message_locale="en",
    )

    with pytest.raises(OSError, match="Dropbox search failed for 'res'"):
        list(client.search("res"))


def test_search_query_over_dropbox_limit_is_rejected_without_calling_dropbox(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """A query longer than Dropbox's 1000-character limit is invalid input, checked locally."""
    with pytest.raises(ValueError, match="at most 1000 characters, got 1001"):
        list(client.search("x" * 1001))

    mock_dbx.files_search_v2.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (
            stone_validators.ValidationError("query too long"),
            ValueError,
            "Dropbox rejected the search query 'report' as invalid",
        ),
        (
            exceptions.RateLimitError("req-1"),
            ConnectionError,
            "Dropbox is unreachable or busy, so the search for 'report' was not run",
        ),
        (
            requests.exceptions.ConnectionError("connection refused"),
            ConnectionError,
            "Dropbox is unreachable or busy, so the search for 'report' was not run",
        ),
        (
            exceptions.InternalServerError("req-1", 500, "oops"),
            OSError,
            "Dropbox search failed for 'report'",
        ),
    ],
)
def test_search_failures_get_a_message_that_matches_their_kind(
    client: DropboxClient,
    mock_dbx: Mock,
    error: Exception,
    expected: type[Exception],
    message: str,
) -> None:
    """Each kind of search failure says what went wrong; invalid input is not called an outage."""
    mock_dbx.files_search_v2.side_effect = error

    with pytest.raises(expected) as raised:
        list(client.search("report"))

    assert type(raised.value) is expected
    assert str(raised.value).startswith(message)


def test_search_failure_message_does_not_echo_a_long_query(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """A long query is shortened in the message instead of repeated in full."""
    mock_dbx.files_search_v2.side_effect = exceptions.InternalServerError("req-1", 500, "oops")
    query = "q" * 400

    with pytest.raises(OSError, match="Dropbox search failed for") as raised:
        list(client.search(query))

    assert query not in str(raised.value)
    assert "(400 characters)" in str(raised.value)


def test_read_operations_translate_permissions_and_connection_errors(
    client: DropboxClient,
    mock_dbx: Mock,
) -> None:
    """Read operations map rate limits and restricted content to ConnectionError / PermissionError."""
    # Rate limit error -> ConnectionError
    mock_dbx.files_get_metadata.side_effect = dropbox.exceptions.RateLimitError("req-1")
    with pytest.raises(ConnectionError, match="Dropbox is unreachable or busy"):
        client.get_metadata("/file.txt")

    # Restricted content -> PermissionError
    error = files.GetMetadataError.path(files.LookupError.restricted_content)
    mock_dbx.files_get_metadata.side_effect = ApiError("123", error, "Restricted", "en")
    with pytest.raises(PermissionError, match=r"Dropbox does not allow reading '/file.txt'"):
        client.get_metadata("/file.txt")
