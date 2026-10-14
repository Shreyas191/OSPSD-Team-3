"""Malformed entry paths are rejected with 422 on every route, before reaching storage.

Dropbox rejects a trailing slash or an empty segment as ``malformed_path``, an error the
client does not translate, so these paths used to surface as 500s. ``AbsolutePath`` in
``app.py`` now refuses them at request validation.
"""

from collections.abc import Iterator

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from httpx import Response
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app, get_storage_client

MALFORMED_PATHS = [
    "/Docs/a.txt/",  # trailing slash
    "/Docs//a.txt",  # empty segment
    "//",  # only slashes
]


@pytest.fixture
def storage() -> Iterator[FakeStorageClient]:
    """Install a fake holding one folder and one file."""
    client = FakeStorageClient()
    client.add("/Docs", is_folder=True)
    client.add("/Docs/a.txt")
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return an HTTP client for the app."""
    return TestClient(app)


def _send(http: TestClient, route: str, path: str) -> Response:
    """Call ``route`` with ``path`` in the field that route takes it from."""
    valid = "/Docs/a.txt"
    requests = {
        "GET /files/metadata": lambda: http.get("/files/metadata", params={"path": path}),
        "GET /files/download": lambda: http.get("/files/download", params={"path": path}),
        "DELETE /files": lambda: http.delete("/files", params={"path": path}),
        "POST /files/rename": lambda: http.post(
            "/files/rename",
            json={"path": path, "new_name": "b.txt"},
        ),
        "POST /files/move (from)": lambda: http.post(
            "/files/move",
            json={"from_path": path, "to_path": "/b.txt"},
        ),
        "POST /files/move (to)": lambda: http.post(
            "/files/move",
            json={"from_path": valid, "to_path": path},
        ),
        "POST /files/copy (from)": lambda: http.post(
            "/files/copy",
            json={"from_path": path, "to_path": "/b.txt"},
        ),
        "POST /files/copy (to)": lambda: http.post(
            "/files/copy",
            json={"from_path": valid, "to_path": path},
        ),
        "POST /folders/create": lambda: http.post("/folders/create", json={"remote_path": path}),
        "POST /files/upload": lambda: http.post(
            "/files/upload",
            files={"file": ("a.txt", b"data")},
            data={"remote_path": path},
        ),
    }
    response: Response = requests[route]()
    return response


ROUTES = [
    "GET /files/metadata",
    "GET /files/download",
    "DELETE /files",
    "POST /files/rename",
    "POST /files/move (from)",
    "POST /files/move (to)",
    "POST /files/copy (from)",
    "POST /files/copy (to)",
    "POST /folders/create",
    "POST /files/upload",
]


@pytest.mark.parametrize("path", MALFORMED_PATHS)
@pytest.mark.parametrize("route", ROUTES)
def test_malformed_path_returns_422_and_changes_nothing(
    storage: FakeStorageClient,
    http: TestClient,
    route: str,
    path: str,
) -> None:
    """A trailing slash or empty segment is a validation error, not a provider call."""
    before = dict(storage.entries)

    response = _send(http, route, path)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert storage.entries == before


def test_list_folder_still_accepts_trailing_slash(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Listing keeps its own pattern, which allows the root and a trailing slash."""
    response = http.get("/folders/list", params={"path": "/Docs/"})

    assert response.status_code == status.HTTP_200_OK
    assert [entry["path"] for entry in response.json()] == ["/Docs/a.txt"]
