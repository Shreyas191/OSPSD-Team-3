"""Tests for Dropbox client upload functionality."""
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import dropbox

from dropbox_client_impl.dropbox_impl import DropboxClient


def test_client_upload_file(tmp_path: Path) -> None:
    """Upload a local file and return its metadata. This test uses a mock Dropbox client to avoid actual network calls."""
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"sample PDF bytes")

    mock_dbx = cast(dropbox.Dropbox, Mock(spec=dropbox.Dropbox))
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
