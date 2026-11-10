"""The FastAPI application: builds the storage client and puts the service together.

A request goes: :mod:`overload` (limit on requests in flight) → a route in :mod:`routes`
→ a ``Client`` method → the provider. The client raises built-in exceptions, which
:mod:`errors` turns into the shared error body. Request and response bodies are in
:mod:`models`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import cloud_storage_client_api
from dropbox_client_impl import DropboxAuthError, DropboxClient
from fastapi import FastAPI

from cloud_storage_service.dependencies import get_storage_client
from cloud_storage_service.models import FileResponse
from cloud_storage_service.overload import ConcurrencyLimitMiddleware, max_in_flight_from_env
from cloud_storage_service.routes import router

__all__ = [
    "ConfigurationError",
    "FileResponse",
    "app",
    "build_storage_client",
    "check_configuration",
    "get_storage_client",
]


class ConfigurationError(RuntimeError):
    """Required configuration, such as a provider credential, is missing or unusable.

    The message names the settings involved and never contains their values, so it is safe
    to log.
    """


def build_storage_client() -> cloud_storage_client_api.Client:
    """Build the storage client the service runs against.

    This is the only place the service names a provider: to switch providers, return a
    different ``Client`` implementation here. Building a ``DropboxClient`` makes no network
    call, but it fails if no Dropbox credentials are configured, so the service stops at
    startup instead of on its first request.

    Raises:
        ConfigurationError: If the provider's credentials are missing or unreadable.

    """
    try:
        return DropboxClient()
    except DropboxAuthError as e:
        # The provider's message already names only the missing settings; `from None` keeps
        # its internals out of the startup log.
        raise ConfigurationError(str(e)) from None


def check_configuration() -> None:
    """Fail fast, before the server starts, if required configuration is missing.

    It builds the storage client once and discards it. Building makes no network call, so
    this checks that the settings are present, not that the provider accepts them; that is
    ``GET /health/provider``'s job. A provider outage therefore never stops the service
    from starting.

    Raises:
        ConfigurationError: If a required setting is missing or unusable.

    """
    build_storage_client()


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
