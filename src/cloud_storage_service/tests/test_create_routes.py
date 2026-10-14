"""HTTP-layer tests for app.py.

The storage client is replaced with a mock through ``app.dependency_overrides``, so these
tests need no Dropbox credentials and no network.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from cloud_storage_service.app import FileResponse, app, get_storage_client

MODIFIED = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def make_file(path: str = "/Docs/report.pdf", *, is_folder: bool = False) -> SimpleNamespace:
    """A stand-in for any ``cloud_storage_client_api.File`` implementation."""
    return SimpleNamespace(
        id="id:abc123",
        name=path.rsplit("/", 1)[-1],
        path=path,
        is_folder=is_folder,
        size=None if is_folder else 2048,
        modified=None if is_folder else MODIFIED,
    )


@pytest.fixture
def storage():
    """A mock storage client injected in place of the real one."""
    mock = MagicMock()
    app.dependency_overrides[get_storage_client] = lambda: mock
    yield mock
    app.dependency_overrides.clear()


@pytest.fixture
def http() -> TestClient:
    # Deliberately not used as a context manager: that would run the lifespan and try to
    # build a real DropboxClient.
    return TestClient(app)


# --- FileResponse.from_file: domain File -> HTTP response model -----------------------


def test_from_file_maps_every_field():
    response = FileResponse.from_file(make_file())
    assert response.model_dump() == {
        "id": "id:abc123",
        "name": "report.pdf",
        "path": "/Docs/report.pdf",
        "is_folder": False,
        "size": 2048,
        "modified": MODIFIED,
    }


def test_from_file_folder_has_null_size_and_modified():
    response = FileResponse.from_file(make_file("/Docs", is_folder=True))
    assert response.is_folder is True
    assert response.size is None
    assert response.modified is None


# --- JSON routes: rename, move, copy, create folder -----------------------------------

# (url, request body, client method). The body's values are exactly the positional
# arguments the route should pass to that client method, in order.
JSON_ROUTES = [
    ("/files/rename", {"path": "/Docs/a.pdf", "new_name": "b.pdf"}, "rename"),
    ("/files/move", {"from_path": "/Docs/a.pdf", "to_path": "/Archive/a.pdf"}, "move"),
    ("/files/copy", {"from_path": "/Docs/a.pdf", "to_path": "/Copy/a.pdf"}, "copy_file"),
    ("/folders/copy", {"from_path": "/Docs", "to_path": "/Docs2"}, "copy_folder"),
    ("/folders/create", {"remote_path": "/Docs/New"}, "create_folder"),
]
ROUTES_MAPPING_VALUE_ERROR = {"rename", "copy_file", "copy_folder"}


@pytest.mark.parametrize("url, body, method", JSON_ROUTES)
def test_success_returns_file_metadata(http, storage, url, body, method):
    getattr(storage, method).return_value = make_file()

    response = http.post(url, json=body)

    assert response.status_code == 200
    assert response.json()["id"] == "id:abc123"
    assert response.json()["path"] == "/Docs/report.pdf"
    getattr(storage, method).assert_called_once_with(*body.values())


@pytest.mark.parametrize("url, body, method", JSON_ROUTES)
@pytest.mark.parametrize(
    "error, expected_status",
    [(FileNotFoundError, 404), (FileExistsError, 409)],
)
def test_client_errors_map_to_http_status(
    http, storage, url, body, method, error, expected_status
):
    getattr(storage, method).side_effect = error("boom")

    response = http.post(url, json=body)

    assert response.status_code == expected_status
    assert response.json()["detail"] == "boom"


@pytest.mark.parametrize(
    "url, body, method",
    [r for r in JSON_ROUTES if r[2] in ROUTES_MAPPING_VALUE_ERROR],
)
def test_value_error_maps_to_422(http, storage, url, body, method):
    getattr(storage, method).side_effect = ValueError("wrong type")

    response = http.post(url, json=body)

    assert response.status_code == 422
    assert response.json()["detail"] == "wrong type"


@pytest.mark.parametrize(
    "url, body",
    [
        ("/files/rename", {"path": "relative.pdf", "new_name": "b.pdf"}),
        ("/files/rename", {"path": "/Docs/a.pdf", "new_name": "x/y.pdf"}),
        ("/files/rename", {"path": "/Docs/a.pdf", "new_name": ""}),
        ("/files/move", {"from_path": "/a", "to_path": "no-leading-slash"}),
        ("/files/copy", {"from_path": "/a"}),
        ("/folders/create", {"remote_path": "NoSlash"}),
    ],
)
def test_invalid_request_is_rejected_before_reaching_client(http, storage, url, body):
    response = http.post(url, json=body)

    assert response.status_code == 422
    assert storage.method_calls == []


# --- Upload (multipart) -----------------------------------------------------------------


def post_upload(http: TestClient, **form_fields: str):
    data = {"remote_path": "/Docs/report.pdf", **form_fields}
    return http.post(
        "/files/upload",
        files={"file": ("report.pdf", b"%PDF-data")},
        data=data,
    )


def test_upload_passes_sent_bytes_to_client_and_cleans_up(http, storage):
    seen = {}

    def fake_upload(local_path, remote_path, *, overwrite=False):
        # Read inside the call: the temp file is deleted once the route returns.
        seen["bytes"] = Path(local_path).read_bytes()
        seen["local_path"] = local_path
        return make_file()

    storage.upload_file.side_effect = fake_upload

    response = post_upload(http, overwrite="true")

    assert response.status_code == 200
    assert response.json()["name"] == "report.pdf"
    assert seen["bytes"] == b"%PDF-data"
    assert not Path(seen["local_path"]).exists()
    args, kwargs = storage.upload_file.call_args
    assert args[1] == "/Docs/report.pdf"
    assert kwargs == {"overwrite": True}


def test_upload_overwrite_defaults_to_false(http, storage):
    storage.upload_file.return_value = make_file()

    post_upload(http)

    assert storage.upload_file.call_args.kwargs == {"overwrite": False}


def test_upload_existing_destination_returns_409(http, storage):
    storage.upload_file.side_effect = FileExistsError("already there")

    response = post_upload(http)

    assert response.status_code == 409


def test_upload_rejects_invalid_remote_path(http, storage):
    response = post_upload(http, remote_path="relative.pdf")

    assert response.status_code == 422
    storage.upload_file.assert_not_called()


def test_upload_requires_a_file(http, storage):
    response = http.post("/files/upload", data={"remote_path": "/Docs/report.pdf"})

    assert response.status_code == 422
    storage.upload_file.assert_not_called()