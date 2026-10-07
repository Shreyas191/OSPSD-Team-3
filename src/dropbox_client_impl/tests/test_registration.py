"""Tests for the registration helper exposed by ``dropbox_client_impl``."""

import importlib
from pathlib import Path

import cloud_storage_client_api
import pytest

import dropbox_client_impl


def test_register_binds_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling register wires the client factory to the Dropbox implementation."""
    client_contract = importlib.import_module("cloud_storage_client_api.client")

    # Reset to the contract default before invoking register.
    monkeypatch.setattr(cloud_storage_client_api, "get_client", client_contract.get_client)

    dropbox_client_impl.register()

    assert cloud_storage_client_api.get_client is dropbox_client_impl.get_client_impl


def test_get_client_impl_returns_dropbox_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The factory builds a DropboxClient."""
    monkeypatch.delenv("DROPBOX_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("DROPBOX_TOKEN_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "token")

    client = dropbox_client_impl.get_client_impl()

    assert isinstance(client, dropbox_client_impl.DropboxClient)
