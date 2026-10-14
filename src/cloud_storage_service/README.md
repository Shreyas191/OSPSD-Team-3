# Cloud Storage Service

## Overview
`cloud_storage_service` is a FastAPI app that exposes the `cloud_storage_client_api.Client` contract over HTTP. Routes only talk to the abstract `Client`; a running service acts on the Dropbox account you logged in with (see the root README).

## Running locally
```bash
uv run uvicorn cloud_storage_service.app:app --reload
```
Interactive docs (Swagger UI) are served at `http://127.0.0.1:8000/docs`. If Dropbox credentials are missing, the service stops at startup with a `DropboxAuthError` telling you to run `uv run python -m dropbox_client_impl login`.

## Choosing the provider
`build_storage_client()` in `app.py` is the only place the service names a concrete provider (it returns a `DropboxClient`). The app calls it once at startup, stores the client on `app.state`, and routes receive it through the `get_storage_client` dependency as an abstract `Client`. Switching providers means changing `build_storage_client()` only; routes and response models stay the same.

## Conventions
- Paths are absolute within the user's storage and start with `/` (e.g. `/Docs/report.pdf`). They go in the JSON body, not the URL, because they contain `/`.
- Successful responses return a **File** object:

  | Field | Type | Meaning |
  |-------|------|---------|
  | `id` | string | Provider's stable identifier for the entry |
  | `name` | string | Last path segment, including any extension |
  | `path` | string | Full path, e.g. `/Docs/report.pdf` |
  | `is_folder` | bool | `true` for folders |
  | `size` | int or null | Size in bytes; `null` for folders |
  | `modified` | datetime or null | Last modified time (ISO 8601); `null` for folders |

- Errors return `{"detail": "<message>"}` with one of the status codes listed per endpoint.

## Create endpoints

### `POST /files/upload`
Upload a file. The request is `multipart/form-data`, not JSON.

- `file` (required): the file contents.
- `remote_path` (required): absolute destination path, including the file name.
- `overwrite` (optional, default `false`): replace an existing file at `remote_path`.

| Status | Meaning |
|--------|---------|
| `200` | Uploaded; body is the File. An overwritten file keeps its `id`. |
| `409` | Something already exists at `remote_path` and `overwrite` is `false` |
| `422` | Invalid form (relative `remote_path`, missing `file`) |

```bash
curl -X POST http://127.0.0.1:8000/files/upload \
  -F "file=@report.pdf" -F "remote_path=/Docs/report.pdf" -F "overwrite=false"
```

The whole file is read into memory and sent in one Dropbox request, which Dropbox limits to 150 MB.

### `POST /folders/create`
Create a folder. Missing parent folders are created too.

```json
{"remote_path": "/Docs/New"}
```

| Status | Meaning |
|--------|---------|
| `200` | Created; body is the folder (`size` and `modified` are `null`) |
| `409` | Something already exists at `remote_path` |
| `422` | Invalid body (relative path, missing field) |

### `POST /files/copy`
Copy a file, or a folder with its contents, to a new full path. The source is unchanged, the copy gets its own `id`, and existing entries are never overwritten.

```json
{"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"}
```

| Status | Meaning |
|--------|---------|
| `200` | Copied; body is the File at `to_path` |
| `404` | Nothing exists at `from_path` |
| `409` | Something already exists at `to_path`, or `to_path` is inside the folder being copied |
| `422` | Invalid body (relative path, missing field) |

## Update endpoints

### `POST /files/rename`
Rename a file or folder in place. The entry stays in the same folder; only its name changes. Existing entries are never overwritten.

Request body:
```json
{"path": "/Docs/report.pdf", "new_name": "summary.pdf"}
```
- `path` (required): absolute path of the entry to rename.
- `new_name` (required): new name; non-empty and must not contain `/`.

| Status | Meaning |
|--------|---------|
| `200` | Renamed; body is the File at its new path |
| `404` | Nothing exists at `path` |
| `409` | The folder already contains an entry named `new_name` |
| `422` | Invalid body (relative path, empty name, name with `/`, missing field) |

```bash
curl -X POST http://127.0.0.1:8000/files/rename \
  -H 'Content-Type: application/json' \
  -d '{"path": "/Docs/report.pdf", "new_name": "summary.pdf"}'
```

### `POST /files/move`
Move a file or folder (with its contents) to a new full path. The destination can be in another folder and can also change the name. Existing entries are never overwritten.

Request body:
```json
{"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"}
```
- `from_path` (required): absolute path of the entry to move.
- `to_path` (required): full destination path, including the entry's name.

| Status | Meaning |
|--------|---------|
| `200` | Moved; body is the File at `to_path` |
| `404` | Nothing exists at `from_path` |
| `409` | Something already exists at `to_path` |
| `422` | Invalid body (relative path, root as source, missing field) |

```bash
curl -X POST http://127.0.0.1:8000/files/move \
  -H 'Content-Type: application/json' \
  -d '{"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"}'
```

Destinations are compared case-insensitively (Dropbox paths are case-insensitive), so renaming to the current name or to a name that differs only in letter case (e.g. `report.txt` → `Report.txt`) returns `409`.

Both operations change state in the provider. Dropbox implements each as a single `files_move_v2` call with autorename disabled; the entry keeps its Dropbox `id`. Other provider failures (e.g. moving a folder into itself, quota) currently surface as `500`.

## Testing
```bash
uv run pytest src/cloud_storage_service/tests/
```
Route tests replace the client with `app.dependency_overrides[get_storage_client]`, so they need no credentials or network. They use `FakeStorageClient` from [`tests/fakes.py`](../../tests/fakes.py), an in-memory `Client` you seed with `storage.add(path)` and then check through `storage.entries`. It implements only the operations some test needs; when you add routes for another operation, implement that method in the fake following the `Client` contract.
