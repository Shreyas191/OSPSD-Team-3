"""Unit tests for ``DropboxClient.search``.

Covers the ``files_search_v2`` call and its options, pagination with
``files_search_continue_v2``, the ``max_results`` limit, skipping matches that are not a live
file or folder, argument validation that never contacts Dropbox, and how each failure becomes
the exception the ``Client`` contract documents (a long query is shortened in the message).
The SDK is an autospec fake, so no network or credentials are needed.
"""

from unittest.mock import MagicMock, call

import pytest
import requests
from dropbox import files, stone_validators
from tests.dropbox_sdk import file_metadata, folder_metadata, rate_limited, server_error

from dropbox_client_impl import DropboxClient


def _match(meta: files.Metadata) -> files.SearchMatchV2:
    """Return a search match for ``meta``."""
    return files.SearchMatchV2(metadata=files.MetadataV2.metadata(meta))


def _result(
    matches: list[files.SearchMatchV2],
    *,
    has_more: bool = False,
    cursor: str = "c1",
) -> files.SearchV2Result:
    """Return one page of search results."""
    return files.SearchV2Result(matches=matches, has_more=has_more, cursor=cursor)


def test_search_passes_query_and_options(client: DropboxClient, dbx: MagicMock) -> None:
    """Search sends the query with max_results and filename_only, and yields matched files."""
    dbx.files_search_v2.return_value = _result([_match(file_metadata("/match.txt"))])

    results = list(client.search("match", max_results=10))

    dbx.files_search_v2.assert_called_once_with(
        query="match",
        options=files.SearchOptions(max_results=10, filename_only=True),
    )
    dbx.files_search_continue_v2.assert_not_called()
    assert [(r.name, r.is_folder) for r in results] == [("match.txt", False)]


def test_search_yields_folders(client: DropboxClient, dbx: MagicMock) -> None:
    """A matched folder is yielded with no size or modification time."""
    dbx.files_search_v2.return_value = _result([_match(folder_metadata("/Matched"))])

    [result] = client.search("Matched")

    assert (result.name, result.is_folder, result.size, result.modified) == (
        "Matched",
        True,
        None,
        None,
    )


def test_search_with_no_matches(client: DropboxClient, dbx: MagicMock) -> None:
    """No matches yields nothing and needs no continuation call."""
    dbx.files_search_v2.return_value = _result([])

    assert list(client.search("nothing", max_results=5)) == []

    dbx.files_search_v2.assert_called_once_with(
        query="nothing",
        options=files.SearchOptions(max_results=5, filename_only=True),
    )
    dbx.files_search_continue_v2.assert_not_called()


def test_search_skips_matches_that_are_not_live_entries(
    client: DropboxClient,
    dbx: MagicMock,
) -> None:
    """Non-metadata matches and deleted entries are skipped; live files are yielded."""
    deleted = files.DeletedMetadata(name="gone.txt", path_display="/gone.txt")
    dbx.files_search_v2.return_value = _result(
        [
            files.SearchMatchV2(metadata=files.MetadataV2.other),
            _match(deleted),
            _match(file_metadata("/active.txt")),
        ],
    )

    assert [r.name for r in client.search("a")] == ["active.txt"]


def test_search_follows_every_page(client: DropboxClient, dbx: MagicMock) -> None:
    """Search continues with each page's cursor until has_more is False."""
    dbx.files_search_v2.return_value = _result(
        [_match(file_metadata("/r1.txt"))],
        has_more=True,
        cursor="c1",
    )
    dbx.files_search_continue_v2.side_effect = [
        _result([_match(file_metadata("/r2.txt"))], has_more=True, cursor="c2"),
        _result([_match(file_metadata("/r3.txt"))]),
    ]

    results = list(client.search("r", max_results=10))

    dbx.files_search_v2.assert_called_once()
    assert dbx.files_search_continue_v2.call_args_list == [call("c1"), call("c2")]
    assert [r.path for r in results] == ["/r1.txt", "/r2.txt", "/r3.txt"]


def test_search_stops_at_max_results(client: DropboxClient, dbx: MagicMock) -> None:
    """Search yields at most max_results entries and fetches no further pages."""
    dbx.files_search_v2.return_value = _result(
        [_match(file_metadata(f"/r{i}.txt")) for i in range(3)],
        has_more=True,
    )

    assert len(list(client.search("r", max_results=2))) == 2

    dbx.files_search_continue_v2.assert_not_called()


@pytest.mark.parametrize(
    ("query", "max_results", "message"),
    [
        ("", 10, "Search query must be non-empty"),
        ("x" * 1001, 10, "at most 1000 characters, got 1001"),
        ("q", 0, "max_results must be greater than 0 and at most 1000, got 0"),
        ("q", -1, "got -1"),
        ("q", 1001, "got 1001"),
        ("q", 5000, "got 5000"),
    ],
)
def test_search_rejects_invalid_arguments_without_calling_dropbox(
    client: DropboxClient,
    dbx: MagicMock,
    query: str,
    max_results: int,
    message: str,
) -> None:
    """An empty or too long query, or max_results outside 1..1000, is rejected locally."""
    with pytest.raises(ValueError, match=message):
        list(client.search(query, max_results=max_results))

    dbx.files_search_v2.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (
            stone_validators.ValidationError("query too long"),
            ValueError,
            "Dropbox rejected 'report' as invalid",
        ),
        (rate_limited(), ConnectionError, "Dropbox is unreachable or busy while searching for"),
        (
            requests.exceptions.ConnectionError("refused"),
            ConnectionError,
            "Dropbox is unreachable or busy while searching for 'report'",
        ),
        (server_error(), OSError, "Dropbox failed searching for 'report'"),
    ],
)
def test_search_translates_errors_on_the_first_page(
    client: DropboxClient,
    dbx: MagicMock,
    error: Exception,
    expected: type[Exception],
    message: str,
) -> None:
    """Each kind of failure has its own exception; invalid input is not called an outage."""
    dbx.files_search_v2.side_effect = error

    with pytest.raises(expected, match=message) as raised:
        list(client.search("report"))

    assert type(raised.value) is expected


def test_search_translates_errors_on_a_later_page(client: DropboxClient, dbx: MagicMock) -> None:
    """A failed continuation becomes OSError naming the query."""
    dbx.files_search_v2.return_value = _result([_match(file_metadata("/r1.txt"))], has_more=True)
    dbx.files_search_continue_v2.side_effect = server_error()

    with pytest.raises(OSError, match="Dropbox failed searching for 'res'"):
        list(client.search("res"))

    dbx.files_search_continue_v2.assert_called_once_with("c1")


def test_search_error_shortens_a_long_query(client: DropboxClient, dbx: MagicMock) -> None:
    """A long query appears as its first 50 characters and its length, not in full."""
    dbx.files_search_v2.side_effect = server_error()
    query = "q" * 400

    with pytest.raises(OSError, match="Dropbox failed searching for") as raised:
        list(client.search(query))

    assert query not in str(raised.value)
    assert f"{'q' * 50!r}... (400 characters)" in str(raised.value)
