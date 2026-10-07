"""HTTP service exposing the cloud storage client API over FastAPI."""

from cloud_storage_service.app import app

__all__ = ["app"]
