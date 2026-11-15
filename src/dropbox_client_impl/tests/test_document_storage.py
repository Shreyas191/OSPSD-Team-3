"""Unit tests for ``DropboxDocumentStorage``, our implementation of the shared storage contract.

Three kinds of check, none of which needs network access or credentials:

- behaviour, against the in-memory ``FakeStorageClient`` from ``tests/fakes.py``;
- error translation, with a ``Mock(spec=Client)`` that raises each built-in exception;
- the full path down to the Dropbox SDK, with the autospec ``dbx`` fixture from conftest.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock

import cloud_storage_api
import cloud_storage_client_api
import pytest
from cloud_storage_api import (
    ConflictError,
    DocumentStorage,
    FileInfo,
    InvalidOperationError,
    InvalidRequestError,
    NotFoundError,
    StorageError,
)
from dropbox import files
from dropbox.files import WriteMode
from tests.dropbox_sdk import api_error, file_metadata, rate_limited
from tests.fakes import FIXED_MODIFIED, FakeStorageClient

from dropbox_client_impl import DropboxClient, DropboxDocumentStorage


@pytest.fixture
def fake() -> FakeStorageClient:
    """Return an empty in-memory client."""
    return FakeStorageClient()


@pytest.fixture
def storage(fake: FakeStorageClient) -> DropboxDocumentStorage:
    """Return the shared-contract storage over the in-memory client."""
    return DropboxDocumentStorage(fake)


@pytest.fixture
def provider() -> Mock:
    """Return a mock client a test can make fail with any built-in exception."""
    return Mock(spec=cloud_storage_client_api.Client)


def test_it_implements_the_shared_interface(storage: DropboxDocumentStorage) -> None:
    """Consumers that code against ``cloud_storage_api.DocumentStorage`` can use it."""
    assert isinstance(storage, DocumentStorage)


def test_default_client_is_dropbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no client given, it builds a ``DropboxClient`` from the environment."""
    built = Mock(spec=DropboxClient)
    monkeypatch.setattr("dropbox_client_impl.document_storage.DropboxClient", lambda: built)

    assert DropboxDocumentStorage()._client is built


# ----- Behaviour (in-memory client) -----


def test_create_returns_shared_file_info(storage: DropboxDocumentStorage) -> None:
    """Create returns ``FileInfo`` with every field; ``type`` is only set on list entries."""
    info = storage.create("/Docs/report.pdf", b"12345")

    assert info == FileInfo(
        id=info.id,
        name="report.pdf",
        path="/Docs/report.pdf",
        size=5,
        modified_at=FIXED_MODIFIED.isoformat().replace("+00:00", "Z"),
        etag=info.etag,
        type=None,
    )
    assert info.id is not None
    assert info.etag is not None


def test_create_never_overwrites(storage: DropboxDocumentStorage, fake: FakeStorageClient) -> None:
    """Creating where a file already exists is ``ConflictError``, and the file is unchanged."""
    existing = fake.add("/Docs/report.pdf", size=1)

    with pytest.raises(ConflictError):
        storage.create("/Docs/report.pdf", b"new")

    assert fake.entries["/Docs/report.pdf"] is existing


def test_replace_keeps_the_id_and_changes_the_etag(storage: DropboxDocumentStorage) -> None:
    """Replace updates the size and etag of the same file."""
    created = storage.create("/a.txt", b"old")

    replaced = storage.replace("/a.txt", b"newer")

    assert replaced.id == created.id
    assert replaced.size == len(b"newer")
    assert replaced.etag != created.etag


def test_replace_missing_file_is_not_found_and_creates_nothing(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
) -> None:
    """Replace never creates a file."""
    with pytest.raises(NotFoundError):
        storage.replace("/missing.txt", b"x")

    assert fake.entries == {}


def test_get_metadata_of_a_folder(storage: DropboxDocumentStorage, fake: FakeStorageClient) -> None:
    """A folder has no size, modified time or etag."""
    fake.add("/Docs", is_folder=True)

    info = storage.get_metadata("/Docs")

    assert (info.size, info.modified_at, info.etag, info.type) == (None, None, None, None)


def test_list_files_is_sorted_and_typed(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
) -> None:
    """Listing returns direct children only, sorted by path, each with its ``type``."""
    fake.add("/Docs", is_folder=True)
    fake.add("/Docs/b.txt")
    fake.add("/Docs/a.txt")
    fake.add("/Docs/Sub", is_folder=True)
    fake.add("/Docs/Sub/deep.txt")

    entries = storage.list_files("/Docs")

    assert [(e.path, e.type) for e in entries] == [
        ("/Docs/Sub", "folder"),
        ("/Docs/a.txt", "file"),
        ("/Docs/b.txt", "file"),
    ]


def test_list_files_of_the_root(storage: DropboxDocumentStorage, fake: FakeStorageClient) -> None:
    """``/`` lists the root."""
    fake.add("/top.txt")

    assert [e.path for e in storage.list_files("/")] == ["/top.txt"]


def test_list_files_of_a_file_is_an_invalid_operation(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
) -> None:
    """Only folders can be listed."""
    fake.add("/a.txt")

    with pytest.raises(InvalidOperationError):
        storage.list_files("/a.txt")


def test_move_returns_the_file_at_its_new_path(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
) -> None:
    """A file moves and keeps its id; nothing is left at the old path."""
    original = fake.add("/a.txt")

    moved = storage.move("/a.txt", "/Archive/a.txt")

    assert (moved.path, moved.id) == ("/Archive/a.txt", original.id)
    assert "/a.txt" not in fake.entries


def test_move_onto_an_existing_file_is_a_conflict(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
) -> None:
    """Move never overwrites."""
    a = fake.add("/a.txt")
    b = fake.add("/b.txt")

    with pytest.raises(ConflictError):
        storage.move("/a.txt", "/b.txt")

    assert fake.entries == {"/a.txt": a, "/b.txt": b}


@pytest.mark.parametrize("to_path", ["/a.txt", "/A.TXT"])
def test_move_to_the_same_path_is_an_invalid_operation(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
    to_path: str,
) -> None:
    """A destination equal to the source, ignoring case, is refused before the move."""
    fake.add("/a.txt")

    with pytest.raises(InvalidOperationError):
        storage.move("/a.txt", to_path)

    assert set(fake.entries) == {"/a.txt"}


def test_move_a_folder_is_an_invalid_operation(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
) -> None:
    """Only files move."""
    fake.add("/Docs", is_folder=True)

    with pytest.raises(InvalidOperationError):
        storage.move("/Docs", "/Archive")

    assert set(fake.entries) == {"/Docs"}


def test_move_a_missing_file_is_not_found(storage: DropboxDocumentStorage) -> None:
    """The source must exist."""
    with pytest.raises(NotFoundError):
        storage.move("/missing.txt", "/b.txt")


def test_delete_removes_a_folder_with_its_contents(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
) -> None:
    """Delete removes everything under the folder."""
    fake.add("/Docs", is_folder=True)
    fake.add("/Docs/a.txt")

    storage.delete("/Docs")

    assert fake.entries == {}


def test_delete_the_root_is_an_invalid_operation(storage: DropboxDocumentStorage) -> None:
    """The root is never deleted."""
    with pytest.raises(InvalidOperationError):
        storage.delete("/")


def test_check_health_passes_when_the_provider_answers(storage: DropboxDocumentStorage) -> None:
    """A healthy provider raises nothing."""
    storage.check_health()


def test_check_health_failure_is_provider_unavailable(provider: Mock) -> None:
    """Any provider failure in the health check is ``ProviderUnavailableError``."""
    provider.check_health.side_effect = OSError("revoked credentials")

    with pytest.raises(cloud_storage_api.ProviderUnavailableError):
        DropboxDocumentStorage(provider).check_health()


def test_download_returns_the_exact_bytes(provider: Mock) -> None:
    """Download returns what the client wrote, and leaves no local file behind."""
    written: list[Path] = []

    def download_file(remote_path: str, local_path: str) -> None:
        Path(local_path).write_bytes(b"\x00exact bytes\xff")
        written.append(Path(local_path))

    provider.download_file.side_effect = download_file

    assert DropboxDocumentStorage(provider).download("/a.bin") == b"\x00exact bytes\xff"
    provider.download_file.assert_called_once()
    assert not written[0].exists()


def test_download_a_folder_is_an_invalid_operation(
    storage: DropboxDocumentStorage,
    fake: FakeStorageClient,
) -> None:
    """Only files can be downloaded."""
    fake.add("/Docs", is_folder=True)

    with pytest.raises(InvalidOperationError):
        storage.download("/Docs")


# ----- Input validation: rejected before the provider is called -----

BAD_PATHS = ["", "Docs/a.txt", "/", "/Docs/", "/Docs//a.txt", "/Docs/./a.txt", "/Docs/../a.txt"]


@pytest.mark.parametrize("path", BAD_PATHS)
@pytest.mark.parametrize(
    "call",
    [
        lambda s, p: s.create(p, b"x"),
        lambda s, p: s.replace(p, b"x"),
        lambda s, p: s.download(p),
        lambda s, p: s.get_metadata(p),
        lambda s, p: s.move(p, "/ok.txt"),
        lambda s, p: s.move("/ok.txt", p),
    ],
    ids=["create", "replace", "download", "get_metadata", "move_from", "move_to"],
)
def test_malformed_paths_are_invalid_requests(provider: Mock, path: str, call: object) -> None:
    """A malformed path is ``InvalidRequestError``, and the provider is never contacted."""
    with pytest.raises(InvalidRequestError):
        call(DropboxDocumentStorage(provider), path)  # type: ignore[operator]

    assert provider.mock_calls == []


@pytest.mark.parametrize("path", [p for p in BAD_PATHS if p != "/"])
def test_malformed_list_and_delete_paths_are_invalid_requests(provider: Mock, path: str) -> None:
    """``/`` is valid for listing (and refused for deleting); other malformed paths are 400s."""
    storage = DropboxDocumentStorage(provider)

    with pytest.raises(InvalidRequestError):
        storage.list_files(path)
    with pytest.raises(InvalidRequestError):
        storage.delete(path)

    assert provider.mock_calls == []


# ----- Error translation (built-in exception -> shared error) -----


@pytest.mark.parametrize(
    ("raised", "shared"),
    [
        (
            cloud_storage_client_api.FileTooLargeError("over 150 MB"),
            cloud_storage_api.FileTooLargeError,
        ),
        (ValueError("Dropbox rejected the path"), cloud_storage_api.InvalidOperationError),
        (IsADirectoryError("is a folder"), cloud_storage_api.InvalidOperationError),
        (FileNotFoundError("missing"), cloud_storage_api.NotFoundError),
        (FileExistsError("taken"), cloud_storage_api.ConflictError),
        (PermissionError("not allowed"), cloud_storage_api.PermissionDeniedError),
        (ConnectionError("rate limited"), cloud_storage_api.ProviderUnavailableError),
        (OSError("quota"), cloud_storage_api.ProviderError),
    ],
)
def test_client_exceptions_become_shared_errors(
    provider: Mock,
    raised: Exception,
    shared: type[StorageError],
) -> None:
    """Each built-in exception maps to its shared error, keeping the client's message."""
    provider.upload_file.side_effect = raised

    with pytest.raises(shared) as caught:
        DropboxDocumentStorage(provider).create("/a.txt", b"x")

    assert caught.value.message == str(raised)
    assert caught.value.__cause__ is raised


def test_unexpected_exceptions_are_not_disguised(provider: Mock) -> None:
    """An exception outside the client contract is a bug and propagates unchanged."""
    provider.get_metadata.side_effect = RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        DropboxDocumentStorage(provider).get_metadata("/a.txt")


# ----- Down to the Dropbox SDK -----


def test_create_through_dropbox_sends_the_bytes(client: DropboxClient, dbx: MagicMock) -> None:
    """The bytes reach ``files_upload`` unchanged, and the Dropbox rev becomes the etag."""
    dbx.files_upload.return_value = file_metadata("/Docs/a.txt", size=5, rev="0a1b2c3d4e")

    info = DropboxDocumentStorage(client).create("/Docs/a.txt", b"hello")

    dbx.files_upload.assert_called_once_with(
        b"hello",
        "/Docs/a.txt",
        mode=WriteMode.add,
        autorename=False,
        strict_conflict=True,
    )
    assert (info.path, info.size, info.etag) == ("/Docs/a.txt", 5, "0a1b2c3d4e")


def test_dropbox_failures_become_shared_errors(client: DropboxClient, dbx: MagicMock) -> None:
    """A Dropbox lookup error and rate limiting end up as the shared errors."""
    dbx.files_get_metadata.side_effect = api_error(
        files.GetMetadataError.path(files.LookupError.not_found),
    )
    dbx.users_get_current_account.side_effect = rate_limited()
    storage = DropboxDocumentStorage(client)

    with pytest.raises(NotFoundError):
        storage.get_metadata("/missing.txt")
    with pytest.raises(cloud_storage_api.ProviderUnavailableError):
        storage.check_health()
