"""HTTP contract tests for the read routes.

The storage client is replaced through FastAPI's dependency overrides, either with the
in-memory ``FakeStorageClient`` from ``tests/fakes.py`` or, to simulate provider failures,
a ``Mock(spec=Client)``, so these tests need no network or credentials. They check public
behaviour: status codes, the shared error body (``{"error": {"code", "message"}}``),
response bodies, and structured failure logging.
"""

import logging
from collections.abc import Iterator
from http import HTTPStatus
from unittest.mock import Mock

import pytest
from cloud_storage_client_api import Client
from fastapi.testclient import TestClient
from tests.fakes import FakeStorageClient

from cloud_storage_service.app import app, get_storage_client


@pytest.fixture
def storage() -> Iterator[FakeStorageClient]:
    """Install an empty fake storage client for the duration of a test."""
    client = FakeStorageClient()
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def provider() -> Iterator[Mock]:
    """Install a mock client whose read operations can simulate provider failures."""
    client = Mock(spec=Client)
    app.dependency_overrides[get_storage_client] = lambda: client
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Return an HTTP client for the app."""
    return TestClient(app)


def _failure_log(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    """Return the single warning the service logged for a failed request."""
    [record] = [r for r in caplog.records if r.name == "cloud_storage_service.errors"]
    assert record.levelno == logging.WARNING
    return record


# ----- GET /files/metadata -----


def test_get_metadata_file_returns_200(storage: FakeStorageClient, http: TestClient) -> None:
    """Metadata for an existing file returns 200 and the serialized FileResponse."""
    storage.add("/Docs/report.pdf", size=2048)

    response = http.get("/files/metadata", params={"path": "/Docs/report.pdf"})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": "id:1",
        "name": "report.pdf",
        "path": "/Docs/report.pdf",
        "size": 2048,
        "modified_at": "2026-10-01T12:30:00Z",
    }


def test_get_metadata_folder_returns_200(storage: FakeStorageClient, http: TestClient) -> None:
    """Metadata for an existing folder returns 200 with null size and modified_at."""
    storage.add("/Docs", is_folder=True)

    response = http.get("/files/metadata", params={"path": "/Docs"})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "id": "id:1",
        "name": "Docs",
        "path": "/Docs",
        "size": None,
        "modified_at": None,
    }


def test_get_metadata_missing_path_returns_404(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Requesting metadata for a nonexistent path returns 404 in shared error format."""
    response = http.get("/files/metadata", params={"path": "/Docs/missing.pdf"})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "No file or folder at '/Docs/missing.pdf'",
        },
    }


@pytest.mark.parametrize(
    "bad_path",
    ["", "/", "/Docs/", "/Docs/../report.pdf"],
)
def test_get_metadata_invalid_path_returns_400(
    storage: FakeStorageClient,
    http: TestClient,
    bad_path: str,
) -> None:
    """An empty, root, trailing-slash or ``..`` path returns 400 in the shared error format."""
    response = http.get("/files/metadata", params={"path": bad_path})
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"


def test_get_metadata_relative_path_returns_the_entry(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """A path without the leading ``/`` (``Docs/a.txt``) returns the entry at ``/Docs/a.txt``."""
    storage.add("/Docs/a.txt", size=3)

    response = http.get("/files/metadata", params={"path": "Docs/a.txt"})

    assert response.status_code == HTTPStatus.OK
    assert response.json()["path"] == "/Docs/a.txt"


# ----- GET /files/download -----


def test_download_file_returns_200_and_bytes(storage: FakeStorageClient, http: TestClient) -> None:
    """Downloading an existing file returns 200 and raw file bytes."""
    storage.add("/Docs/report.pdf", size=2048)

    response = http.get("/files/download", params={"path": "/Docs/report.pdf"})

    assert response.status_code == HTTPStatus.OK
    assert response.content == b"content for /Docs/report.pdf"
    assert "application/octet-stream" in response.headers["content-type"]


def test_download_file_missing_returns_404(storage: FakeStorageClient, http: TestClient) -> None:
    """Downloading a nonexistent file returns 404 in shared error format."""
    response = http.get("/files/download", params={"path": "/Docs/missing.pdf"})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "No file at '/Docs/missing.pdf'",
        },
    }


def test_download_folder_returns_422(storage: FakeStorageClient, http: TestClient) -> None:
    """Downloading a folder is an invalid operation: 422 in the shared error format."""
    storage.add("/Docs", is_folder=True)

    response = http.get("/files/download", params={"path": "/Docs"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {
        "error": {
            "code": "invalid_operation",
            "message": "Path is a folder: '/Docs'",
        },
    }


def test_download_file_invalid_path_returns_400(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Downloading with a ``..`` segment in the path returns 400 in shared error format."""
    response = http.get("/files/download", params={"path": "/Docs/../report.pdf"})
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"


# ----- GET /files?path= (list a folder) -----


def test_list_folder_returns_direct_children(storage: FakeStorageClient, http: TestClient) -> None:
    """Listing a folder returns its direct children, not recursively nested entries."""
    storage.add("/Docs", is_folder=True)
    storage.add("/Docs/report.pdf", size=1024)
    storage.add("/Docs/Archive", is_folder=True)
    storage.add("/Docs/Archive/old.pdf", size=512)

    response = http.get("/files", params={"path": "/Docs"})

    assert response.status_code == HTTPStatus.OK
    assert [item["path"] for item in response.json()] == ["/Docs/Archive", "/Docs/report.pdf"]


@pytest.mark.parametrize("root", ["", "/"])
def test_list_folder_root(storage: FakeStorageClient, http: TestClient, root: str) -> None:
    """Both ``path=`` and ``path=/`` list the root folder."""
    storage.add("/a.txt", size=10)
    storage.add("/Docs/b.txt", size=20)

    response = http.get("/files", params={"path": root})

    assert response.status_code == HTTPStatus.OK
    assert [item["path"] for item in response.json()] == ["/Docs", "/a.txt"]


def test_list_folder_without_path_returns_400(storage: FakeStorageClient, http: TestClient) -> None:
    """The ``path`` query parameter is required; omitting it is a 400."""
    response = http.get("/files")

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"


def test_list_folder_is_sorted_by_path(storage: FakeStorageClient, http: TestClient) -> None:
    """Entries come back sorted by path, whatever order the client yields them in."""
    storage.add("/Docs", is_folder=True)
    for name in ["c.txt", "a.txt", "B", "b.txt"]:
        storage.add(f"/Docs/{name}", is_folder=name == "B")

    response = http.get("/files", params={"path": "/Docs"})

    assert response.status_code == HTTPStatus.OK
    assert [item["path"] for item in response.json()] == [
        "/Docs/B",
        "/Docs/a.txt",
        "/Docs/b.txt",
        "/Docs/c.txt",
    ]


def test_list_folder_entries_have_type_and_file_info(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Each entry is the file info plus ``type``; folders have null size and modified_at."""
    storage.add("/Docs", is_folder=True)
    report = storage.add("/Docs/report.pdf", size=1024)
    archive = storage.add("/Docs/Archive", is_folder=True)

    response = http.get("/files", params={"path": "/Docs"})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == [
        {
            "id": archive.id,
            "name": "Archive",
            "path": "/Docs/Archive",
            "size": None,
            "modified_at": None,
            "type": "folder",
        },
        {
            "id": report.id,
            "name": "report.pdf",
            "path": "/Docs/report.pdf",
            "size": 1024,
            "modified_at": "2026-10-01T12:30:00Z",
            "type": "file",
        },
    ]
    assert response.json()[1]["modified_at"].endswith("Z")


def test_list_folder_empty_folder(storage: FakeStorageClient, http: TestClient) -> None:
    """Listing an empty folder returns an empty list."""
    storage.add("/Empty", is_folder=True)

    response = http.get("/files", params={"path": "/Empty"})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == []


def test_list_folder_relative_path_is_accepted(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """A folder path without the leading ``/`` lists the folder at the absolute path."""
    storage.add("/Docs", is_folder=True)
    storage.add("/Docs/report.pdf")

    response = http.get("/files", params={"path": "Docs"})

    assert response.status_code == HTTPStatus.OK
    assert [item["path"] for item in response.json()] == ["/Docs/report.pdf"]


def test_list_folder_missing_returns_404(storage: FakeStorageClient, http: TestClient) -> None:
    """Listing a folder that does not exist returns 404 in shared error format."""
    response = http.get("/files", params={"path": "/Nonexistent"})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "No folder at '/Nonexistent'",
        },
    }


def test_list_folder_target_is_file_returns_422(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Listing a path that points to a file is an invalid operation: 422."""
    storage.add("/file.txt", size=10)

    response = http.get("/files", params={"path": "/file.txt"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {
        "error": {
            "code": "invalid_operation",
            "message": "Path is a file: '/file.txt'",
        },
    }


@pytest.mark.parametrize("path", ["/a/../b", "/Docs/", "/Docs//sub", "//"])
def test_list_folder_invalid_path_returns_400(
    storage: FakeStorageClient,
    http: TestClient,
    path: str,
) -> None:
    """A ``..`` segment, trailing slash or empty segment returns 400 invalid_request."""
    storage.add("/Docs", is_folder=True)

    response = http.get("/files", params={"path": path})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"


def test_list_folder_synthetic_ids_are_stable(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Listing synthetic parent folders repeatedly preserves entry IDs."""
    storage.add("/Docs/report.pdf", size=100)

    first_resp = http.get("/files", params={"path": ""})
    assert first_resp.status_code == HTTPStatus.OK
    first_id = first_resp.json()[0]["id"]

    second_resp = http.get("/files", params={"path": ""})
    assert second_resp.status_code == HTTPStatus.OK
    second_id = second_resp.json()[0]["id"]

    assert first_id == second_id


def test_old_list_route_is_gone(storage: FakeStorageClient, http: TestClient) -> None:
    """``GET /folders/list`` was replaced by ``GET /files?path=`` and now returns 404."""
    response = http.get("/folders/list", params={"path": "/Docs"})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["error"]["code"] == "route_not_found"


# ----- GET /files/search -----


def test_search_files_returns_matching_entries(
    storage: FakeStorageClient,
    http: TestClient,
) -> None:
    """Searching returns entries matching the query."""
    storage.add("/Docs/report_2026.pdf", size=100)
    storage.add("/Docs/summary.pdf", size=200)
    storage.add("/Notes/report_draft.txt", size=50)

    response = http.get("/files/search", params={"q": "report"})

    assert response.status_code == HTTPStatus.OK
    items = response.json()
    assert len(items) == 2
    names = {item["name"] for item in items}
    assert names == {"report_2026.pdf", "report_draft.txt"}


def test_search_files_respects_max_results(storage: FakeStorageClient, http: TestClient) -> None:
    """Search caps results to max_results."""
    storage.add("/Docs/report1.pdf", size=100)
    storage.add("/Docs/report2.pdf", size=100)
    storage.add("/Docs/report3.pdf", size=100)

    response = http.get("/files/search", params={"q": "report", "max_results": 2})

    assert response.status_code == HTTPStatus.OK
    assert len(response.json()) == 2


@pytest.mark.parametrize(
    ("q", "max_results"),
    [
        ("", 10),
        ("report", 0),
        ("report", -5),
        ("report", 1001),
        ("report", 5000),
    ],
)
def test_search_files_invalid_parameters_return_400(
    storage: FakeStorageClient,
    http: TestClient,
    q: str,
    max_results: int,
) -> None:
    """Search returns 400 if q is empty or max_results is not in [1, 1000]."""
    response = http.get("/files/search", params={"q": q, "max_results": max_results})
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"


def test_search_query_over_1000_characters_returns_400_without_reaching_the_client(
    provider: Mock,
    http: TestClient,
) -> None:
    """A query longer than Dropbox's limit is rejected by request validation."""
    response = http.get("/files/search", params={"q": "x" * 1001})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"
    provider.search.assert_not_called()


def test_search_client_value_error_returns_422(http: TestClient) -> None:
    """A ValueError raised by the storage client during search returns 422 in shared error format."""
    mock_client = Mock(spec=Client)
    mock_client.search.side_effect = ValueError("Search query invalid")
    app.dependency_overrides[get_storage_client] = lambda: mock_client
    try:
        response = http.get("/files/search", params={"q": "test"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {
        "error": {
            "code": "invalid_operation",
            "message": "Search query invalid",
        },
    }


# ----- Level 5: Failure Handling & Structured Logging -----


@pytest.mark.parametrize(
    ("route", "params"),
    [
        ("/files/metadata", {"path": "/Docs/report.pdf"}),
        ("/files/download", {"path": "/Docs/report.pdf"}),
        ("/files", {"path": "/Docs"}),
    ],
)
def test_permission_denied_returns_403(
    provider: Mock,
    http: TestClient,
    route: str,
    params: dict[str, str],
) -> None:
    """PermissionError from client becomes 403 with permission_denied error code."""
    provider.get_metadata.side_effect = PermissionError("Dropbox does not allow reading metadata")
    provider.download_file.side_effect = PermissionError("Dropbox does not allow reading file")
    provider.list_folder.side_effect = PermissionError("Dropbox does not allow listing folder")

    response = http.get(route, params=params)

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["error"]["code"] == "permission_denied"


@pytest.mark.parametrize(
    ("route", "params", "method_name"),
    [
        ("/files/metadata", {"path": "/Docs/report.pdf"}, "get_metadata"),
        ("/files/download", {"path": "/Docs/report.pdf"}, "download_file"),
        ("/files", {"path": "/Docs"}, "list_folder"),
        ("/files/search", {"q": "test"}, "search"),
    ],
)
@pytest.mark.parametrize(
    ("error", "expected_status", "code"),
    [
        (ConnectionError("rate_limit"), HTTPStatus.SERVICE_UNAVAILABLE, "provider_unavailable"),
        (OSError("internal_error"), HTTPStatus.BAD_GATEWAY, "provider_error"),
    ],
)
def test_provider_failures_map_to_5xx_without_leaking_details(  # noqa: PLR0913
    provider: Mock,
    http: TestClient,
    route: str,
    params: dict[str, str],
    method_name: str,
    error: OSError,
    expected_status: HTTPStatus,
    code: str,
) -> None:
    """Provider failures map to 502/503 with generic caller-facing message."""
    getattr(provider, method_name).side_effect = error

    response = http.get(route, params=params)

    assert response.status_code == expected_status
    body = response.json()
    assert body["error"]["code"] == code
    assert str(error) not in body["error"]["message"]


def test_read_failure_is_logged_as_structured_warning(
    provider: Mock,
    http: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failed read operation logs one structured warning with details."""
    provider.get_metadata.side_effect = OSError("Dropbox internal error")

    with caplog.at_level(logging.WARNING):
        http.get("/files/metadata", params={"path": "/Docs/report.pdf"})

    record = _failure_log(caplog)
    assert record.__dict__["operation"] == "get_file_metadata"
    assert record.__dict__["status"] == HTTPStatus.BAD_GATEWAY
    assert record.__dict__["code"] == "provider_error"
    assert record.__dict__["provider_failure"] is True
    assert record.__dict__["reason"] == "Dropbox internal error"
