"""How routes get the storage client.

The app builds one client at startup (see ``app.py``) and keeps it on ``app.state``.
Routes receive it through :func:`get_storage_client`, so they only ever see the abstract
``Client``, and tests replace it with ``app.dependency_overrides[get_storage_client]``.
"""

from typing import Annotated

import cloud_storage_client_api
from fastapi import Depends, Request


def get_storage_client(request: Request) -> cloud_storage_client_api.Client:
    """Return the storage client created at startup."""
    client: cloud_storage_client_api.Client = request.app.state.storage_client
    return client


StorageClient = Annotated[cloud_storage_client_api.Client, Depends(get_storage_client)]
