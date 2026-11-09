"""Invalid entry paths are rejected with 400 on every route, before reaching storage.

The vertical's path rule (``clean_path`` in ``models.py``): the leading ``/`` is optional,
and a path must not be empty or the root, end with ``/``, or contain an empty, ``.`` or
``..`` segment. A rejected path is ``400 invalid_request``. The HW1 upload form keeps its
absolute-path pattern, so it rejects relative paths too.
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
# Rejected by ``clean_path`` but not by the upload form's pattern, so not checked on upload.
DOT_SEGMENT_PATHS = ["/Docs/../a.txt", "/Docs/./a.txt", "Docs/.."]


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
def test_malformed_path_returns_400_and_changes_nothing(
    storage: FakeStorageClient,
    http: TestClient,
    route: str,
    path: str,
) -> None:
    """A trailing slash or empty segment is a validation error, not a provider call."""
    before = dict(storage.entries)

    response = _send(http, route, path)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"
    assert storage.entries == before


@pytest.mark.parametrize("path", DOT_SEGMENT_PATHS)
@pytest.mark.parametrize("route", ROUTES)
def test_dot_segment_path_returns_400_and_changes_nothing(
    storage: FakeStorageClient,
    http: TestClient,
    route: str,
    path: str,
) -> None:
    """A ``.`` or ``..`` segment is rejected on every route that takes an entry path."""
    before = dict(storage.entries)

    response = _send(http, route, path)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"
    assert storage.entries == before


def test_metadata_with_relative_path_returns_the_entry(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """``GET /files/metadata?path=Docs/a.txt`` returns the entry at ``/Docs/a.txt``."""
    response = http.get("/files/metadata", params={"path": "Docs/a.txt"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["path"] == "/Docs/a.txt"
    assert response.json()["name"] == "a.txt"


def test_download_with_relative_path_returns_the_file(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """``GET /files/download?path=Docs/a.txt`` downloads ``/Docs/a.txt``."""
    response = http.get("/files/download", params={"path": "Docs/a.txt"})

    assert response.status_code == status.HTTP_200_OK
    assert response.content == b"content for /Docs/a.txt"


def test_upload_with_relative_path_creates_the_absolute_file(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """The upload form follows the same path rule: ``Docs/b.txt`` uploads ``/Docs/b.txt``."""
    response = _send(http, "POST /files/upload", "Docs/b.txt")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["path"] == "/Docs/b.txt"
    assert "/Docs/b.txt" in storage.entries
