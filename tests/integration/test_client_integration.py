"""Integration tests for dropbox_client_impl dependency injection and authentication.

These tests verify that importing the implementation wires it into the abstract
factory, and (when credentials are available) that it can authenticate with Dropbox.
Credentials are optional: a saved OAuth login (``.dropbox_token.json``),
``DROPBOX_REFRESH_TOKEN`` (CI), or ``DROPBOX_ACCESS_TOKEN`` (dev fallback).
"""

import os

import pytest

import cloud_storage_client_api
import dropbox_client_impl  # Import to trigger dependency injection
from dropbox_client_impl import auth

# Mark all tests in this file as integration tests
pytestmark = pytest.mark.integration

HAS_CREDENTIALS = bool(
    os.environ.get("DROPBOX_REFRESH_TOKEN") or os.environ.get("DROPBOX_ACCESS_TOKEN") or auth.get_token_file().exists(),
)


@pytest.mark.circleci
def test_dependency_injection_works() -> None:
    """Importing dropbox_client_impl rebinds the abstract factory."""
    assert cloud_storage_client_api.get_client is dropbox_client_impl.get_client_impl


@pytest.mark.circleci
def test_get_client_returns_dropbox_client() -> None:
    """The factory returns a DropboxClient, or fails clearly without credentials."""
    try:
        client = cloud_storage_client_api.get_client()
    except dropbox_client_impl.DropboxAuthError:
        pytest.skip("Dropbox credentials are not configured in this environment.")

    assert isinstance(client, dropbox_client_impl.DropboxClient)
    assert isinstance(client, cloud_storage_client_api.Client)


@pytest.mark.circleci
@pytest.mark.skipif(not HAS_CREDENTIALS, reason="Dropbox credentials are not configured.")
def test_authenticates_with_dropbox() -> None:
    """With real credentials, a read-only account call succeeds."""
    client = dropbox_client_impl.DropboxClient()

    account = client.dbx.users_get_current_account()

    assert account.account_id


@pytest.mark.circleci
@pytest.mark.skipif(not HAS_CREDENTIALS, reason="Dropbox credentials are not configured.")
def test_get_current_account_over_http() -> None:
    """With real credentials, POST /2/users/get_current_account succeeds."""
    account = dropbox_client_impl.get_current_account()

    assert account["account_id"]
