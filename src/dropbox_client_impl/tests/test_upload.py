from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dropbox_client_impl.dropbox_impl import DropboxClient

def test_client_upload_file(tmp_path) -> None:
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"sample PDF bytes")

    mock_dropbox = Mock()
    mock_dropbox.files_upload.return_value = SimpleNamespace(
        path_display="/Docs/report.pdf",
        path_lower="/docs/report.pdf",
    )
    test_client = SimpleNamespace(_dropbox=mock_dropbox)

    uploaded = DropboxClient.upload_file(
        self = test_client,
        local_path=str(local_file),
        remote_path="/Docs/report.pdf",
    )

    assert uploaded.path == "/Docs/report.pdf"
    mock_dropbox.files_upload.assert_called_once()