"""Integration test for the read operations against a real Dropbox account.

Drives download_file, get_metadata, list_folder, and search through DropboxClient
and the real Dropbox API. Fixtures are created and removed inside a uniquely named
scratch folder, so the test leaves the account unchanged. Runs when any Dropbox
credential is available: a saved OAuth login, ``DROPBOX_REFRESH_TOKEN``, or
``DROPBOX_ACCESS_TOKEN``.
"""

import contextlib
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from cloud_storage_client_api.file import File

from dropbox_client_impl import DropboxClient, auth

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            os.environ.get(auth.REFRESH_TOKEN_ENV_VAR)
            or os.environ.get(auth.ACCESS_TOKEN_ENV_VAR)
            or auth.get_token_file().exists()
        ),
        reason="Dropbox credentials are not configured.",
    ),
]


@pytest.fixture
def client() -> DropboxClient:
    """Return a DropboxClient authenticated from the environment."""
    return DropboxClient()


@pytest.fixture
def scratch(client: DropboxClient) -> Iterator[str]:
    """Create a scratch folder holding ``sample.txt`` and ``FolderA`` with nested file."""
    root = f"/ospsd-it-{uuid.uuid4().hex[:8]}"
    client.dbx.files_upload(b"sample content", f"{root}/sample.txt")
    client.dbx.files_create_folder_v2(f"{root}/FolderA")
    client.dbx.files_upload(b"nested content", f"{root}/FolderA/nested.txt")
    try:
        yield root
    finally:
        with contextlib.suppress(Exception):
            client.dbx.files_delete_v2(root)


def test_get_metadata_file_and_folder(client: DropboxClient, scratch: str) -> None:
    """get_metadata returns File metadata for files and folders, and raises FileNotFoundError when missing."""
    file_meta = client.get_metadata(f"{scratch}/sample.txt")
    assert isinstance(file_meta, File)
    assert file_meta.name == "sample.txt"
    assert file_meta.is_folder is False
    assert file_meta.size == len(b"sample content")
    assert file_meta.modified is not None

    folder_meta = client.get_metadata(f"{scratch}/FolderA")
    assert isinstance(folder_meta, File)
    assert folder_meta.name == "FolderA"
    assert folder_meta.is_folder is True
    assert folder_meta.size is None
    assert folder_meta.modified is None

    with pytest.raises(FileNotFoundError):
        client.get_metadata(f"{scratch}/missing.txt")


def test_download_file_round_trip_and_errors(client: DropboxClient, scratch: str, tmp_path: Path) -> None:
    """download_file downloads file bytes, and raises FileNotFoundError or IsADirectoryError appropriately."""
    local_path = tmp_path / "downloaded.txt"
    res = client.download_file(f"{scratch}/sample.txt", str(local_path))
    assert local_path.read_bytes() == b"sample content"
    assert res.name == "sample.txt"
    assert res.is_folder is False
    assert res.size == len(b"sample content")

    missing_local = tmp_path / "nope.txt"
    with pytest.raises(FileNotFoundError):
        client.download_file(f"{scratch}/missing.txt", str(missing_local))

    folder_local = tmp_path / "folder_download.txt"
    with pytest.raises(IsADirectoryError):
        client.download_file(f"{scratch}/FolderA", str(folder_local))


def test_list_folder_contents_and_errors(client: DropboxClient, scratch: str) -> None:
    """list_folder yields direct children, and raises FileNotFoundError or ValueError."""
    entries = list(client.list_folder(scratch))
    names = {e.name for e in entries}
    assert names == {"sample.txt", "FolderA"}

    with pytest.raises(FileNotFoundError):
        list(client.list_folder(f"{scratch}/nonexistent_folder"))

    with pytest.raises(ValueError, match="file"):
        list(client.list_folder(f"{scratch}/sample.txt"))


def test_search_operations_and_validation(client: DropboxClient) -> None:
    """Search validates parameters and returns an iterator of File entries."""
    with pytest.raises(ValueError, match="Search query must be non-empty"):
        list(client.search(""))

    with pytest.raises(ValueError, match="max_results must be greater than 0"):
        list(client.search("test", max_results=0))

    with pytest.raises(ValueError, match="max_results must be greater than 0"):
        list(client.search("test", max_results=-1))

    with pytest.raises(ValueError, match="max_results must be greater than 0 and at most 1000"):
        list(client.search("test", max_results=5000))

    # A search with an arbitrary unique string should execute and return without errors
    results = list(client.search("nonexistent_unique_query_12345_xyz", max_results=5))
    assert results == []
