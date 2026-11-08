"""End-to-end tests: run ``main.py`` as a separate process, the way a user starts the service."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e

MAIN_SCRIPT = Path(__file__).parent.parent.parent / "main.py"
STARTUP_TIMEOUT_SECONDS = 30
NO_CREDENTIALS = {
    "DROPBOX_REFRESH_TOKEN": "",
    "DROPBOX_ACCESS_TOKEN": "",
    "DROPBOX_APP_KEY": "",
    "DROPBOX_APP_SECRET": "",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _start_service(port: int, extra_env: dict[str, str]) -> subprocess.Popen[str]:
    env = {**os.environ, "PORT": str(port), **extra_env}
    return subprocess.Popen(  # noqa: S603
        [sys.executable, str(MAIN_SCRIPT)],
        cwd=MAIN_SCRIPT.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.fixture
def port() -> int:
    """Return a free local port for the service."""
    return _free_port()


@pytest.fixture
def running_service(port: int) -> Iterator[str]:
    """Start the service with the real credentials and return its base URL once it answers."""
    if not (MAIN_SCRIPT.parent / ".env").exists():
        pytest.skip("No .env file found - cannot run the service against Dropbox")
    process = _start_service(port, {})
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while True:
            try:
                httpx.get(f"{base_url}/openapi.json")
                break
            except httpx.TransportError:
                if time.monotonic() > deadline or process.poll() is not None:
                    pytest.fail(f"The service did not start on {base_url}")
                time.sleep(0.2)
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.circleci
def test_service_without_credentials_stops_with_a_login_hint(port: int, tmp_path: Path) -> None:
    """With no Dropbox credentials the service exits at startup and says how to log in."""
    missing_token_file = str(tmp_path / "no_token.json")
    process = _start_service(port, {**NO_CREDENTIALS, "DROPBOX_TOKEN_FILE": missing_token_file})

    output, _ = process.communicate(timeout=STARTUP_TIMEOUT_SECONDS)

    assert process.returncode != 0
    assert "Dropbox is not authorized" in output
    assert "dropbox_client_impl login" in output


@pytest.mark.local_credentials
def test_service_lists_the_root_folder(running_service: str) -> None:
    """The started service answers a real request against Dropbox."""
    response = httpx.get(f"{running_service}/folders/list", params={"path": ""}, timeout=30)

    assert response.status_code == httpx.codes.OK
    assert isinstance(response.json(), list)
