"""Public exports for the Dropbox client implementation package."""

from dropbox_client_impl.auth import (
    DropboxAPIError,
    DropboxAuthError,
    DropboxConnectionError,
    DropboxError,
    dropbox_request,
    get_current_account,
    get_dropbox_client,
)
from dropbox_client_impl.dropbox_impl import (
    DropboxClient,
    get_client_impl,
    register,
)
from dropbox_client_impl.file_impl import DropboxFile

__all__ = [
    "DropboxAPIError",
    "DropboxAuthError",
    "DropboxClient",
    "DropboxConnectionError",
    "DropboxError",
    "DropboxFile",
    "dropbox_request",
    "get_client_impl",
    "get_current_account",
    "get_dropbox_client",
    "register",
]


# Dependency Injection happens at import time
register()
