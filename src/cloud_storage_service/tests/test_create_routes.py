"""HTTP-layer tests for app.py.

The storage client is replaced with a mock through ``app.dependency_overrides``, so these
tests need no Dropbox credentials and no network.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import cloud_storage_client_api
import pytest
from fastapi import status
from fastapi.testclient import TestClient
from httpx import Response

from cloud_storage_service.app import FileResponse, app, get_storage_client

MODIFIED = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def make_file(
    path: str = "/Docs/report.pdf",
    *,
    is_folder: bool = False,
) -> cloud_storage_client_api.File:
    """Build a stand-in for any ``cloud_storage_client_api.File`` implementation."""
    return cast(
        "cloud_storage_client_api.File",
        SimpleNamespace(
            id="id:abc123",
            name=path.rsplit("/", 1)[-1],
            path=path,
            is_folder=is_folder,
            size=None if is_folder else 2048,
            modified=None if is_folder else MODIFIED,
        ),
    )


@pytest.fixture
def storage() -> Iterator[MagicMock]:
    """Inject a mock storage client in place of the real one."""
    mock = MagicMock()
    app.dependency_overrides[get_storage_client] = lambda: mock
    yield mock
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    """Create a client without starting the real Dropbox lifespan."""
    # Deliberately not used as a context manager: that would run the lifespan and try to
    # build a real DropboxClient.
    return TestClient(app)


# --- FileResponse.from_file: domain File -> HTTP response model -----------------------


def test_from_file_maps_every_field() -> None:
    """Map every domain file field into the response model."""
    response = FileResponse.from_file(make_file())
    assert response.model_dump() == {
        "id": "id:abc123",
        "name": "report.pdf",
        "path": "/Docs/report.pdf",
        "is_folder": False,
        "size": 2048,
        "modified": MODIFIED,
    }


def test_from_file_folder_has_null_size_and_modified() -> None:
    """Keep folder size and modification time null in the response."""
    response = FileResponse.from_file(make_file("/Docs", is_folder=True))
    assert response.is_folder is True
    assert response.size is None
    assert response.modified is None


# --- JSON routes: rename, move, copy, create folder -----------------------------------

# (url, request body, client method). The body's values are exactly the positional
# arguments the route should pass to that client method, in order.
JSON_ROUTES: tuple[tuple[str, dict[str, str], str], ...] = (
    ("/files/rename", {"path": "/Docs/a.pdf", "new_name": "b.pdf"}, "rename"),
    ("/files/move", {"from_path": "/Docs/a.pdf", "to_path": "/Archive/a.pdf"}, "move"),
    ("/files/copy", {"from_path": "/Docs/a.pdf", "to_path": "/Copy/a.pdf"}, "copy_file"),
    ("/folders/copy", {"from_path": "/Docs", "to_path": "/Docs2"}, "copy_folder"),
    ("/folders/create", {"remote_path": "/Docs/New"}, "create_folder"),
)
ROUTES_MAPPING_VALUE_ERROR = {"rename", "copy_file", "copy_folder", "create_folder"}


@pytest.mark.parametrize(("url", "body", "method"), JSON_ROUTES)
def test_success_returns_file_metadata(
    http: TestClient,
    storage: MagicMock,
    url: str,
    body: dict[str, str],
    method: str,
) -> None:
    """Return file metadata after a successful JSON route call."""
    getattr(storage, method).return_value = make_file()

    response = http.post(url, json=body)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == "id:abc123"
    assert response.json()["path"] == "/Docs/report.pdf"
    getattr(storage, method).assert_called_once_with(*body.values())


@pytest.mark.parametrize(
    ("route", "error", "expected_status"),
    [
        (route, error, expected_status)
        for route in JSON_ROUTES
        for error, expected_status in (
            (FileNotFoundError, status.HTTP_404_NOT_FOUND),
            (FileExistsError, status.HTTP_409_CONFLICT),
        )
    ],
)
def test_client_errors_map_to_http_status(
    http: TestClient,
    storage: MagicMock,
    route: tuple[str, dict[str, str], str],
    error: type[Exception],
    expected_status: int,
) -> None:
    """Map client not-found and conflict errors to their HTTP status codes."""
    url, body, method = route
    getattr(storage, method).side_effect = error("boom")

    response = http.post(url, json=body)

    assert response.status_code == expected_status
    assert response.json()["detail"] == "boom"


@pytest.mark.parametrize(
    ("url", "body", "method"),
    [r for r in JSON_ROUTES if r[2] in ROUTES_MAPPING_VALUE_ERROR],
)
def test_value_error_maps_to_422(
    http: TestClient,
    storage: MagicMock,
    url: str,
    body: dict[str, str],
    method: str,
) -> None:
    """Map client value errors to HTTP 422 responses."""
    getattr(storage, method).side_effect = ValueError("wrong type")

    response = http.post(url, json=body)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["detail"] == "wrong type"


@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("/files/rename", {"path": "relative.pdf", "new_name": "b.pdf"}),
        ("/files/rename", {"path": "/Docs/a.pdf", "new_name": "x/y.pdf"}),
        ("/files/rename", {"path": "/Docs/a.pdf", "new_name": ""}),
        ("/files/move", {"from_path": "/a", "to_path": "no-leading-slash"}),
        ("/files/copy", {"from_path": "/a"}),
        ("/folders/create", {"remote_path": "NoSlash"}),
    ],
)
def test_invalid_request_is_rejected_before_reaching_client(
    http: TestClient,
    storage: MagicMock,
    url: str,
    body: dict[str, str],
) -> None:
    """Reject malformed JSON requests before calling the storage client."""
    response = http.post(url, json=body)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert storage.method_calls == []


# --- Upload (multipart) -----------------------------------------------------------------


def post_upload(http: TestClient, **form_fields: str) -> Response:
    """Post a multipart upload request with the supplied form fields."""
    data = {"remote_path": "/Docs/report.pdf", **form_fields}
    response: Response = http.post(
        "/files/upload",
        files={"file": ("report.pdf", b"%PDF-data")},
        data=data,
    )
    return response


def test_upload_passes_sent_bytes_to_client_and_cleans_up(
    http: TestClient,
    storage: MagicMock,
) -> None:
    """Pass uploaded bytes to the client and remove the temporary file."""
    seen: dict[str, Any] = {}

    def fake_upload(
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = False,
    ) -> cloud_storage_client_api.File:
        """Capture uploaded bytes while the route's temporary file exists."""
        # Read inside the call: the temp file is deleted once the route returns.
        seen["bytes"] = Path(local_path).read_bytes()
        seen["local_path"] = local_path
        return make_file()

    storage.upload_file.side_effect = fake_upload

    response = post_upload(http, overwrite="true")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "report.pdf"
    assert seen["bytes"] == b"%PDF-data"
    assert not Path(seen["local_path"]).exists()
    args, kwargs = storage.upload_file.call_args
    assert args[1] == "/Docs/report.pdf"
    assert kwargs == {"overwrite": True}


def test_upload_overwrite_defaults_to_false(
    http: TestClient,
    storage: MagicMock,
) -> None:
    """Use false as the default upload overwrite setting."""
    storage.upload_file.return_value = make_file()

    post_upload(http)

    assert storage.upload_file.call_args.kwargs == {"overwrite": False}


def test_upload_existing_destination_returns_409(
    http: TestClient,
    storage: MagicMock,
) -> None:
    """Return HTTP 409 when upload finds an existing destination."""
    storage.upload_file.side_effect = FileExistsError("already there")

    response = post_upload(http)

    assert response.status_code == status.HTTP_409_CONFLICT


def test_upload_client_value_error_maps_to_422(
    http: TestClient,
    storage: MagicMock,
) -> None:
    """Map an upload value error to an HTTP 422 response."""
    storage.upload_file.side_effect = ValueError("invalid upload")

    response = post_upload(http)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["detail"] == "invalid upload"


def test_upload_rejects_invalid_remote_path(
    http: TestClient,
    storage: MagicMock,
) -> None:
    """Reject an invalid remote path before calling the storage client."""
    response = post_upload(http, remote_path="relative.pdf")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    storage.upload_file.assert_not_called()


def test_upload_requires_a_file(http: TestClient, storage: MagicMock) -> None:
    """Reject an upload request that omits the file field."""
    response = http.post("/files/upload", data={"remote_path": "/Docs/report.pdf"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    storage.upload_file.assert_not_called()
