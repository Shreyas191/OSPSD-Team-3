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
- Paths are absolute within the user's storage and start with `/` (e.g. `/Docs/report.pdf`). They must not end with `/` or contain empty segments (`/Docs//report.pdf`); such paths are rejected with `422`. `POST` routes take paths in the request body; `GET` and `DELETE` routes take a URL-encoded `path` query parameter. `GET /folders/list` also accepts `""` or `/` for the root.
- Successful responses return a **File** object:

  | Field | Type | Meaning |
  |-------|------|---------|
  | `id` | string | Provider's stable identifier for the entry |
  | `name` | string | Last path segment, including any extension |
  | `path` | string | Full path, e.g. `/Docs/report.pdf` |
  | `is_folder` | bool | `true` for folders |
  | `size` | int or null | Size in bytes; `null` for folders |
  | `modified` | datetime or null | Last modified time (ISO 8601); `null` for folders |

- Errors return one of the status codes listed per endpoint in the team's shared error format: `{"error": {"code": "<code>", "message": "<message>"}}`.

### Shared error format
The format is defined in `errors.py`: a route opts in by being registered on the `shared_errors` router in `app.py`, without its own `try`/`except`. Request validation errors and the built-in exceptions the `Client` contract documents are mapped as follows; any other exception is a bug and still surfaces as `500`.

| Status | `code` | Cause |
|--------|--------|-------|
| `422` | `invalid_input` | Request validation failed, or the client raised `ValueError` or `IsADirectoryError` |
| `404` | `not_found` | `FileNotFoundError` |
| `409` | `conflict` | `FileExistsError`: the destination is already taken |
| `403` | `permission_denied` | `PermissionError`: the provider does not allow the operation on this entry |
| `502` | `provider_failure` | Any other `OSError`: the provider failed |
| `503` | `provider_unavailable` | `ConnectionError`: the provider is unreachable or busy; try again later |

`502` and `503` responses carry a fixed message, so provider details never reach the caller. Every failure is logged as a warning on the `cloud_storage_service.errors` logger, as `key=value` pairs that are also attached to the log record as attributes:

```text
Request failed: operation='delete_entry' status=502 code='provider_failure' provider_failure=True reason="Dropbox failed to delete '/Docs' (DeleteError('too_many_files', None))"
```

The log never includes credentials, and validation failures record which parameter was wrong but not the value sent.

## Create endpoints

### `POST /files/upload`
Upload a file. The request is `multipart/form-data`, not JSON.

- `file` (required): the file contents.
- `remote_path` (required): absolute destination path, including the file name.
- `overwrite` (optional, default `false`): replace an existing file at `remote_path`.

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Uploaded; body is the File. An overwritten file keeps its `id`. |
| `403` | `permission_denied` | The provider does not allow writing at `remote_path` (e.g. no write permission, or a team folder) |
| `409` | `conflict` | Something already exists at `remote_path` and `overwrite` is `false`, or a folder is there |
| `422` | `invalid_input` | Invalid form (relative `remote_path`, missing `file`), a path the provider rejects, or a file over 150 MB |
| `502` | `provider_failure` | The provider failed (e.g. the account is out of space, or the service's Dropbox credentials have expired) |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later. After a lost connection the upload may already have happened, so a retry without `overwrite` can return `409` |

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

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Created; body is the folder (`size` and `modified` are `null`) |
| `403` | `permission_denied` | The provider does not allow creating a folder there |
| `409` | `conflict` | Something already exists at `remote_path` |
| `422` | `invalid_input` | Invalid body (relative path, missing field), or a path the provider rejects |
| `502` | `provider_failure` | The provider failed (e.g. the account is out of space) |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

### `POST /files/copy`
Copy a file, or a folder with its contents, to a new full path. The source is unchanged, the copy gets its own `id`, and existing entries are never overwritten.

```json
{"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"}
```

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Copied; body is the File at `to_path` |
| `403` | `permission_denied` | The provider does not allow the copy (e.g. no write permission, or shared-folder restrictions) |
| `404` | `not_found` | Nothing exists at `from_path` |
| `409` | `conflict` | Something already exists at `to_path`, including the source itself |
| `422` | `invalid_input` | Invalid body (relative path, missing field), or `to_path` is inside `from_path` (a folder copied into itself) |
| `502` | `provider_failure` | The provider failed (e.g. the account is out of space) |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl -X POST http://127.0.0.1:8000/files/copy \
  -H 'Content-Type: application/json' \
  -d '{"from_path": "/Docs", "to_path": "/Docs/Backup"}'
# 422 response body:
# {"error": {"code": "invalid_input", "message": "Cannot copy '/Docs' into itself: '/Docs/Backup' is inside the source"}}
```

## Update endpoints

### `POST /files/rename`
Rename a file or folder in place. The entry stays in the same folder; only its name changes. Existing entries are never overwritten.

Request body:
```json
{"path": "/Docs/report.pdf", "new_name": "summary.pdf"}
```
- `path` (required): absolute path of the entry to rename.
- `new_name` (required): new name; non-empty and must not contain `/`.

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Renamed; body is the File at its new path |
| `403` | `permission_denied` | The provider does not allow the rename (e.g. the entry is locked or read-only for this account) |
| `404` | `not_found` | Nothing exists at `path` |
| `409` | `conflict` | The folder already contains an entry named `new_name`, including the entry itself |
| `422` | `invalid_input` | Invalid body (relative path, empty name, name with `/`, missing field) |
| `502` | `provider_failure` | The provider failed (e.g. the service's Dropbox credentials have expired) |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

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

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Moved; body is the File at `to_path` |
| `403` | `permission_denied` | The provider does not allow the move (e.g. no write permission, or moving a shared folder somewhere it can't go) |
| `404` | `not_found` | Nothing exists at `from_path`, including an entry that was already moved |
| `409` | `conflict` | Something already exists at `to_path`, including the entry itself |
| `422` | `invalid_input` | Invalid body (relative path, root as source, missing field), or `to_path` is inside `from_path` (a folder moved into itself) |
| `502` | `provider_failure` | The provider failed (e.g. the account is out of space, or the service's Dropbox credentials have expired) |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl -X POST http://127.0.0.1:8000/files/move \
  -H 'Content-Type: application/json' \
  -d '{"from_path": "/Docs/report.pdf", "to_path": "/Archive/report.pdf"}'
# 409 response body when /Archive/report.pdf already exists:
# {"error": {"code": "conflict", "message": "Something already exists at '/Archive/report.pdf'"}}
```

Destinations are compared case-insensitively (Dropbox paths are case-insensitive), so renaming to the current name or to a name that differs only in letter case (e.g. `report.txt` → `Report.txt`) returns `409`.

Both operations change state in the provider. Dropbox implements each as a single `files_move_v2` call with autorename disabled; the entry keeps its Dropbox `id`. Neither is idempotent: repeating a successful rename or move returns `404`, because nothing is left at the source. That also makes a retry after a `503` safe: if the first attempt did go through, the retry returns `404` instead of moving anything twice.

## Read endpoints

### `GET /files/metadata`
Get metadata for a single file or folder by absolute path.

Query parameters:
- `path` (required): absolute path of the entry (e.g. `?path=/Docs/report.pdf`).

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Success; body is the File object |
| `403` | `permission_denied` | The provider does not allow reading metadata for the entry |
| `404` | `not_found` | Nothing exists at `path` |
| `422` | `invalid_input` | Invalid query parameter (missing `path`, relative or root path) |
| `502` | `provider_failure` | The provider failed |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl "http://127.0.0.1:8000/files/metadata?path=/Docs/report.pdf"
```

### `GET /files/download`
Download the raw bytes of the file located at `path`.

Query parameters:
- `path` (required): absolute path of the remote file to download (e.g. `?path=/Docs/report.pdf`).

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Success; body contains raw file bytes (`application/octet-stream`) |
| `403` | `permission_denied` | The provider does not allow reading the file |
| `404` | `not_found` | Nothing exists at `path` |
| `422` | `invalid_input` | Path points to a folder, or invalid query parameter |
| `502` | `provider_failure` | The provider failed |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl -o downloaded.pdf "http://127.0.0.1:8000/files/download?path=/Docs/report.pdf"
```

### `GET /folders/list`
List all direct children in the folder at `path`. Returns an empty list or root entries if `path` is empty or root (`/`).

Query parameters:
- `path` (optional, defaults to empty): absolute path of the folder to list (e.g. `?path=/Docs`), or empty for root. Must start with `/` or be empty.

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Success; body is a JSON array of File objects |
| `403` | `permission_denied` | The provider does not allow listing the folder |
| `404` | `not_found` | Folder does not exist |
| `422` | `invalid_input` | Path points to a file rather than a folder, or invalid relative path |
| `502` | `provider_failure` | The provider failed |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl "http://127.0.0.1:8000/folders/list?path=/Docs"
```

### `GET /files/search`
Search for files and folders matching a query string, up to `max_results` entries.

Query parameters:
- `q` (required): search query, 1 to 1000 characters (Dropbox's limit) (e.g. `?q=report`).
- `max_results` (optional, defaults to 10): maximum number of results to return (must be between 1 and 1000).

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Success; body is a JSON array of matching File objects |
| `422` | `invalid_input` | Invalid query (`q` empty or over 1000 characters, or `max_results` not between 1 and 1000) |
| `502` | `provider_failure` | The provider failed |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl "http://127.0.0.1:8000/files/search?q=report&max_results=5"
```

## Delete endpoints

### `DELETE /files`
Delete a file, or a folder together with everything inside it.

Query parameters:
- `path` (required): absolute path of the entry to delete (e.g. `?path=/Docs/report.pdf`). The root (`/`) can never be deleted.

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `204` | | Deleted; the response has no body |
| `403` | `permission_denied` | The provider does not allow deleting the entry (e.g. it is locked, a team folder, or read-only for this account) |
| `404` | `not_found` | Nothing exists at `path`, including a path that was already deleted |
| `422` | `invalid_input` | Invalid query parameter (missing, empty, relative, or the root `/`), or a path the provider rejects as malformed |
| `502` | `provider_failure` | The provider failed (e.g. the folder holds too many files to delete in one call, or the service's Dropbox credentials have expired) |
| `503` | `provider_unavailable` | The provider is unreachable or busy (e.g. too many write operations in progress); nothing was deleted, so try again later |

```bash
curl -X DELETE "http://127.0.0.1:8000/files?path=/Docs/report.pdf"
# 404 response body:
# {"error": {"code": "not_found", "message": "No file or folder at '/Docs/report.pdf'"}}
```

Delete is not idempotent: deleting the same path twice returns `204` the first time and `404` the second. The response has no body because the `Client.delete` contract returns only `True`, not the deleted entry. The root path is rejected before the provider is contacted. Dropbox implements delete as a single `files_delete_v2` call; deleted entries go to the Dropbox account's trash, but this service does not offer restore.


## Testing
```bash
uv run pytest src/cloud_storage_service/tests/
```
Route tests replace the client with `app.dependency_overrides[get_storage_client]`, so they need no credentials or network. They use `FakeStorageClient` from [`tests/fakes.py`](../../tests/fakes.py), an in-memory `Client` you seed with `storage.add(path)` and then check through `storage.entries`. It implements only the operations some test needs; when you add routes for another operation, implement that method in the fake following the `Client` contract.
