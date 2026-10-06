"""Tests for the registration helper exposed by ``dropbox_client_impl``."""

import importlib

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


def test_get_client_impl_returns_dropbox_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory builds a DropboxClient."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "token")

    client = dropbox_client_impl.get_client_impl()

    assert isinstance(client, dropbox_client_impl.DropboxClient)
