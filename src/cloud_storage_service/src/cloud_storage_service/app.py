"""The FastAPI application: builds the storage client and puts the service together.

A request goes: :mod:`overload` (limit on requests in flight) → a route in :mod:`routes`
→ a ``Client`` method → the provider. The client raises built-in exceptions, which
:mod:`errors` turns into the shared error body. Request and response bodies are in
:mod:`models`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import cloud_storage_client_api
from dropbox_client_impl import DropboxClient
from fastapi import FastAPI

from cloud_storage_service.dependencies import get_storage_client
from cloud_storage_service.errors import add_error_handlers
from cloud_storage_service.models import FileResponse
from cloud_storage_service.overload import ConcurrencyLimitMiddleware, max_in_flight_from_env
from cloud_storage_service.routes import router

__all__ = ["FileResponse", "app", "build_storage_client", "get_storage_client"]


def build_storage_client() -> cloud_storage_client_api.Client:
    """Build the storage client the service runs against.

    This is the only place the service names a provider: to switch providers, return a
    different ``Client`` implementation here. Building a ``DropboxClient`` makes no network
    call, but it raises ``DropboxAuthError`` if no Dropbox credentials are configured, so
    the service fails at startup instead of on its first request.
    """
    return DropboxClient()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the storage client when the app starts; it lives as long as the app."""
    app.state.storage_client = build_storage_client()
    yield


app = FastAPI(title="Cloud Storage Service", lifespan=lifespan)
# At most MAX_IN_FLIGHT_REQUESTS requests run at once; the rest get 503 service_busy at once
# instead of queueing (see overload.py).
app.add_middleware(ConcurrencyLimitMiddleware, max_in_flight=max_in_flight_from_env())
app.include_router(router)
add_error_handlers(app)
