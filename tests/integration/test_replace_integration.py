"""Integration tests for ``DropboxClient.replace_file`` and ``check_health`` against real Dropbox.

Everything happens inside a uniquely named scratch folder that is deleted afterwards, so the
account is left unchanged. Runs when any Dropbox credential is available: a saved OAuth
login, ``DROPBOX_REFRESH_TOKEN`` or ``DROPBOX_ACCESS_TOKEN``.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import dropbox
import pytest
from dropbox import files
from dropbox.files import WriteMode

from dropbox_client_impl import DropboxClient, auth, errors

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

ORIGINAL = b"original content"
NEW = b"replacement content, longer than the original"


@pytest.fixture
def client() -> DropboxClient:
    """Return a DropboxClient authenticated from the environment."""
    return DropboxClient()


@pytest.fixture
def scratch(client: DropboxClient) -> Iterator[str]:
    """Return the path of an empty scratch folder, deleted with its contents afterwards."""
    root = f"/ospsd-it-{uuid.uuid4().hex[:8]}"
    client.dbx.files_create_folder_v2(root)
    yield root
    client.dbx.files_delete_v2(root)


def _local(tmp_path: Path, content: bytes, name: str = "local.txt") -> str:
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def _download(client: DropboxClient, path: str) -> bytes:
    _, response = client.dbx.files_download(path)
    return bytes(response.content)


def test_replace_existing_file_changes_content_and_keeps_id(client: DropboxClient, scratch: str, tmp_path: Path) -> None:
    """Replacing an existing file changes its bytes and size but keeps its Dropbox id."""
    path = f"{scratch}/report.txt"
    original = client.upload_file(_local(tmp_path, ORIGINAL), path)

    replaced = client.replace_file(_local(tmp_path, NEW, "new.txt"), path)

    assert replaced.id == original.id
    assert (replaced.path, replaced.size) == (path, len(NEW))
    assert original.size == len(ORIGINAL)
    assert _download(client, path) == NEW


def test_replace_missing_file_raises_and_creates_nothing(client: DropboxClient, scratch: str, tmp_path: Path) -> None:
    """Replacing a path with no file raises FileNotFoundError and leaves the path empty."""
    path = f"{scratch}/missing.txt"

    with pytest.raises(FileNotFoundError):
        client.replace_file(_local(tmp_path, NEW), path)

    with pytest.raises(FileNotFoundError):
        client.get_metadata(path)


def test_replace_against_stale_revision_is_a_conflict(
    client: DropboxClient,
    scratch: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WriteMode.update upload with an outdated rev is refused by Dropbox, and replace says FileExistsError.

    Observed against real Dropbox (2026-11-08): ``files_upload(..., mode=WriteMode.update(old_rev),
    autorename=False)`` raises ``dropbox.exceptions.ApiError`` whose ``error`` is
    ``UploadError.path(UploadWriteFailed(reason=WriteError.conflict(WriteConflictError.file)))``,
    i.e. tag ``path/conflict/file``. The file keeps the newer content.
    """
    path = f"{scratch}/report.txt"
    client.upload_file(_local(tmp_path, ORIGINAL), path)
    stale = client.dbx.files_get_metadata(path)
    old_rev = stale.rev
    client.upload_file(_local(tmp_path, NEW, "new.txt"), path, overwrite=True)

    with pytest.raises(dropbox.exceptions.ApiError) as raised:
        client.dbx.files_upload(b"x", path, mode=WriteMode.update(old_rev), autorename=False)

    reason = raised.value.error
    assert isinstance(reason, files.UploadError)
    assert reason.is_path()
    write = reason.get_path().reason
    assert write.is_conflict()
    assert write.get_conflict().is_file()
    assert isinstance(errors.translate(raised.value, errors.REPLACE, path), FileExistsError)

    # The same race through the public method: it reads the stale metadata, then uploads.
    monkeypatch.setattr(client, "_file_metadata", lambda _path: stale)
    with pytest.raises(FileExistsError, match="changed while it was being replaced"):
        client.replace_file(_local(tmp_path, b"x", "x.txt"), path)

    assert _download(client, path) == NEW


def test_check_health_succeeds_against_real_dropbox(client: DropboxClient) -> None:
    """With valid credentials the health check reaches Dropbox and returns without raising."""
    client.check_health()
