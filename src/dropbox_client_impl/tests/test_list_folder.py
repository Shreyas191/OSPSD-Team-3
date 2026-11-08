"""Unit tests for ``DropboxClient.list_folder``.

Covers pagination with ``files_list_folder`` and ``files_list_folder_continue`` at the
boundaries (no entries, fewer than a page, exactly a page, more than a page), listing the
root, and how failures on the first page and on later pages become the exceptions the
``Client`` contract documents. The SDK is an autospec fake, so no network or credentials are
needed.
"""

from unittest.mock import MagicMock

import pytest
from dropbox import files
from tests.dropbox_sdk import api_error, file_metadata, folder_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient


def _page(paths: list[str], *, has_more: bool, cursor: str = "c1") -> files.ListFolderResult:
    """Return one page of a folder listing holding a file for each path."""
    return files.ListFolderResult(
        entries=[file_metadata(p) for p in paths],
        cursor=cursor,
        has_more=has_more,
    )


def test_list_folder_with_no_entries(client: DropboxClient, dbx: MagicMock) -> None:
    """An empty folder yields nothing and needs no continuation call."""
    dbx.files_list_folder.return_value = _page([], has_more=False)

    assert list(client.list_folder("/empty")) == []

    dbx.files_list_folder.assert_called_once_with("/empty")
    dbx.files_list_folder_continue.assert_not_called()


def test_list_folder_with_fewer_entries_than_a_page(client: DropboxClient, dbx: MagicMock) -> None:
    """A single partial page yields its files and folders without a continuation call."""
    dbx.files_list_folder.return_value = files.ListFolderResult(
        entries=[file_metadata("/test/a.txt"), folder_metadata("/test/sub")],
        cursor="c1",
        has_more=False,
    )

    results = list(client.list_folder("/test"))

    dbx.files_list_folder.assert_called_once_with("/test")
    dbx.files_list_folder_continue.assert_not_called()
    assert [(r.path, r.is_folder) for r in results] == [("/test/a.txt", False), ("/test/sub", True)]


def test_list_folder_with_exactly_a_page(client: DropboxClient, dbx: MagicMock) -> None:
    """A full page with has_more set is followed by one (empty) continuation page."""
    paths = [f"/test/file{i}.txt" for i in range(3)]
    dbx.files_list_folder.return_value = _page(paths, has_more=True, cursor="c-exact")
    dbx.files_list_folder_continue.return_value = _page([], has_more=False)

    results = list(client.list_folder("/test"))

    dbx.files_list_folder.assert_called_once_with("/test")
    dbx.files_list_folder_continue.assert_called_once_with("c-exact")
    assert [r.path for r in results] == paths


def test_list_folder_with_more_than_a_page(client: DropboxClient, dbx: MagicMock) -> None:
    """Entries from every page are yielded once each, in order."""
    dbx.files_list_folder.return_value = _page(["/p/1.txt", "/p/2.txt"], has_more=True)
    dbx.files_list_folder_continue.return_value = _page(["/p/3.txt", "/p/4.txt"], has_more=False)

    results = list(client.list_folder("/p"))

    dbx.files_list_folder.assert_called_once_with("/p")
    dbx.files_list_folder_continue.assert_called_once_with("c1")
    assert [r.path for r in results] == ["/p/1.txt", "/p/2.txt", "/p/3.txt", "/p/4.txt"]


def test_list_folder_defaults_to_the_root(client: DropboxClient, dbx: MagicMock) -> None:
    """Without a path the root is listed, which Dropbox names with an empty string."""
    dbx.files_list_folder.return_value = _page([], has_more=False)

    assert list(client.list_folder()) == []

    dbx.files_list_folder.assert_called_once_with("")


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (
            api_error(files.ListFolderError.path(files.LookupError.not_found)),
            FileNotFoundError,
            "No file or folder at '/x'",
        ),
        (
            api_error(files.ListFolderError.path(files.LookupError.not_folder)),
            ValueError,
            "Path is a file: '/x'",
        ),
        (
            api_error(files.ListFolderError.path(files.LookupError.restricted_content)),
            PermissionError,
            "Dropbox does not allow listing '/x'",
        ),
        (rate_limited(), ConnectionError, "Dropbox is unreachable or busy while listing '/x'"),
        (server_error(), OSError, "Dropbox failed listing '/x'"),
    ],
)
def test_list_folder_translates_errors_on_the_first_page(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
    message: str,
) -> None:
    """A failed first page becomes the contract exception that names the folder."""
    dbx.files_list_folder.side_effect = error

    with pytest.raises(expected, match=message) as raised:
        list(client.list_folder("/x"))

    assert type(raised.value) is expected


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (
            api_error(files.ListFolderContinueError.path(files.LookupError.not_found)),
            FileNotFoundError,
            "No file or folder at '/x'",
        ),
        (
            api_error(files.ListFolderContinueError.reset),
            OSError,
            "Dropbox invalidated the cursor while listing '/x'",
        ),
        (rate_limited(), ConnectionError, "Dropbox is unreachable or busy while listing '/x'"),
        (server_error(), OSError, "Dropbox failed listing '/x'"),
    ],
)
def test_list_folder_translates_errors_on_a_later_page(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
    message: str,
) -> None:
    """A failed continuation becomes the contract exception after the first page is yielded."""
    dbx.files_list_folder.return_value = _page(["/x/1.txt"], has_more=True)
    dbx.files_list_folder_continue.side_effect = error
    listing = client.list_folder("/x")

    assert next(listing).path == "/x/1.txt"
    with pytest.raises(expected, match=message) as raised:
        next(listing)

    assert type(raised.value) is expected
    dbx.files_list_folder_continue.assert_called_once_with("c1")
