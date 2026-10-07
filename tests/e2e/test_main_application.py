"""End-to-End tests for the main application.

This module tests the application's main entry point (main.py) as a black box.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import main
from dropbox_client_impl import DropboxClient

# Mark all tests in this file as e2e tests
pytestmark = pytest.mark.e2e

MAIN_SCRIPT = Path(__file__).parent.parent.parent / "main.py"


@pytest.mark.circleci
def test_main_runs_with_mocked_client(capsys: pytest.CaptureFixture[str]) -> None:
    """main() completes using a DropboxClient backed by a mocked SDK."""
    # Jing: eventually should lift this mock of dropbox out so other tests can reuse it
    mock_dbx = Mock() # mock of dropbox
    mock_dbx.files_list_folder.return_value = Mock(entries=[], has_more=False) # mock of ListFolderResult
    client = DropboxClient(dbx=mock_dbx)

    with patch("cloud_storage_client_api.get_client", return_valuesoles=client):
        main.main()

    assert "Demo complete." in capsys.readouterr().out


@pytest.mark.local_credentials
def test_main_script_runs_against_dropbox() -> None:
    """Runs main.py as a subprocess against the real Dropbox account.

    Requires DROPBOX_* credentials in the environment or a local .env file.
    """
    env_file = MAIN_SCRIPT.parent / ".env"
    if not env_file.exists():
        pytest.skip("No .env file found - cannot run E2E test against Dropbox")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(MAIN_SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=str(MAIN_SCRIPT.parent),
    )

    assert "Demo complete." in result.stdout
