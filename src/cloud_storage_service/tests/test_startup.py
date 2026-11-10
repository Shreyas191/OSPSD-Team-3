"""Tests for the startup configuration check (``check_configuration``).

The check builds the real storage client, which makes no network call, so these tests need
no network and no real credentials. Each test sets the Dropbox variables itself and points
the token file at an empty ``tmp_path``, so a developer's own login never leaks in.
"""

from pathlib import Path

import pytest

from cloud_storage_service.app import ConfigurationError, check_configuration

APP_KEY = "fake-app-key-value"
APP_SECRET = "fake-app-secret-value"
REFRESH_TOKEN = "fake-refresh-token-value"
CREDENTIALS = {
    "DROPBOX_APP_KEY": APP_KEY,
    "DROPBOX_APP_SECRET": APP_SECRET,
    "DROPBOX_REFRESH_TOKEN": REFRESH_TOKEN,
}


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Start every test with no Dropbox credentials in the environment or on disk."""
    for name in [*CREDENTIALS, "DROPBOX_ACCESS_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DROPBOX_TOKEN_FILE", str(tmp_path / "no-token.json"))


def test_check_configuration_passes_when_every_credential_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the three deployment variables set, the check passes without calling Dropbox."""
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)

    check_configuration()


@pytest.mark.parametrize("missing", list(CREDENTIALS))
def test_check_configuration_names_the_missing_variable_and_no_values(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """A missing variable stops startup with a message that names it and shows no value."""
    for name, value in CREDENTIALS.items():
        if name != missing:
            monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=missing) as exc_info:
        check_configuration()

    message = str(exc_info.value)
    for value in CREDENTIALS.values():
        assert value not in message


def test_check_configuration_with_nothing_set_names_every_variable() -> None:
    """With no credentials at all, one message lists all three variables to set."""
    with pytest.raises(ConfigurationError) as exc_info:
        check_configuration()

    message = str(exc_info.value)
    for name in CREDENTIALS:
        assert name in message
