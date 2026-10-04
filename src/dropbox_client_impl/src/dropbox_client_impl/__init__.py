"""Public exports for the Dropbox client implementation package."""

from dropbox_client_impl.dropbox_impl import (
    DropboxClient,
    get_client_impl,
    register,
)
from dropbox_client_impl.file_impl import DropboxFile

__all__ = [
    "DropboxClient",
    "DropboxFile",
    "get_client_impl",
    "register",
]


# Dependency Injection happens at import time
register()
