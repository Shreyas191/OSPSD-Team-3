# Cloud Storage Service

## Overview
`cloud_storage_service` is a FastAPI app that exposes the `cloud_storage_client_api.Client` contract over HTTP. Routes only talk to the abstract `Client`; a running service acts on the Dropbox account you logged in with (see the root README).

## Running locally
```bash
uv run python main.py                                      # from the repository root
uv run uvicorn cloud_storage_service.app:app --reload      # while developing
```
Interactive docs (Swagger UI) are served at `http://127.0.0.1:8000/docs`. If Dropbox credentials are missing, the service stops at startup with a `DropboxAuthError` telling you to run `uv run python -m dropbox_client_impl login`.

The service handles at most `MAX_IN_FLIGHT_REQUESTS` requests at once (default `10`, set in `.env` or the environment); any more get `503 service_busy` immediately. See [overload protection](../../docs/overload-protection.md).

## Modules

| Module | Owns |
|--------|------|
| `app.py` | Builds the storage client at startup and puts the app together |
| `routes.py` | One function per route, each calling one `Client` method |
| `models.py` | Request and response bodies, and the path pattern |
| `dependencies.py` | `get_storage_client`, which hands routes the client |
| `errors.py` | The shared error format and the exception-to-status mapping |
| `overload.py` | The limit on requests in flight |

## Choosing the provider
`build_storage_client()` in `app.py` is the only place the service names a concrete provider (it returns a `DropboxClient`). The app calls it once at startup, stores the client on `app.state`, and routes receive it through the `get_storage_client` dependency as an abstract `Client`. Switching providers means changing `build_storage_client()` only; routes and response models stay the same.

## Conventions
- Paths follow the vertical's rule: the leading `/` is optional (`Docs/report.pdf` means `/Docs/report.pdf`), and a path must not be empty, end with `/`, or contain an empty, `.` or `..` segment (`/Docs//a`, `/Docs/../a`). Such paths are rejected with `400 invalid_request` before the provider is called. `POST` routes take paths in the request body; `GET`, `PATCH` and `DELETE` routes take a URL-encoded `path` query parameter. `GET /files` also accepts `""` or `/` for the root.
- Successful responses return the vertical's **file info** object:

  | Field | Type | Meaning |
  |-------|------|---------|
  | `id` | string | Provider's stable identifier; unchanged by move, rename and replace |
  | `name` | string | Last path segment, including any extension |
  | `path` | string | Full path, e.g. `/Docs/report.pdf` |
  | `size` | int or null | Size in bytes; `null` for folders |
  | `modified_at` | string or null | Last modified time, ISO 8601 in UTC ending in `Z` (`2026-10-23T12:00:00Z`); `null` for folders |

  Entries of a folder listing also have `type`: `"file"` or `"folder"`.

- Errors return one of the status codes listed per endpoint in the team's shared error format: `{"error": {"code": "<code>", "message": "<message>"}}`.

### Shared error format
The format is defined in `errors.py`: every route is registered on the `router` in `routes.py`, which uses `SharedErrorRoute`, so no route has its own `try`/`except`. App-level handlers give the same body to requests no route handles. Every error the service returns uses this body:

| Status | `code` | Cause |
|--------|--------|-------|
| `400` | `invalid_request` | Request validation failed: missing field, invalid path, body that is not JSON |
| `422` | `invalid_operation` | A valid request the operation refuses: the client raised `ValueError` or `IsADirectoryError` (e.g. a folder moved into itself, deleting the root, downloading a folder) |
| `404` | `not_found` | `FileNotFoundError` |
| `409` | `conflict` | `FileExistsError`: the destination is already taken |
| `403` | `permission_denied` | `PermissionError`: the provider does not allow the operation on this entry |
| `502` | `provider_error` | Any other `OSError`: the provider failed |
| `503` | `provider_unavailable` | `ConnectionError`: the provider is unreachable or busy; try again later |
| `503` | `service_busy` | Overload protection: the service already has `MAX_IN_FLIGHT_REQUESTS` requests in flight, so this one was rejected without contacting the provider. Comes with `Retry-After: 1`; retrying is always safe. Applies to every endpoint. See [overload protection](../../docs/overload-protection.md). |
| `404` | `route_not_found` | No such route (a missing file is `not_found` instead) |
| `405` | `method_not_allowed` | The route exists but not with this method; the `Allow` header lists the methods it accepts |
| `500` | `internal_error` | An unexpected exception (a bug); the message is fixed and the details go to the log |

`500`, `502` and `503` responses carry a fixed message, so provider details never reach the caller. Every failure is logged as a warning on the `cloud_storage_service.errors` logger, as `key=value` pairs that are also attached to the log record as attributes:

```text
Request failed: operation='delete_entry' status=502 code='provider_error' provider_failure=True reason="Dropbox failed deleting '/Docs' (DeleteError('too_many_files', None))"
```

The log never includes credentials, and validation failures record which parameter was wrong but not the value sent.

## Create endpoints

### `POST /files`
Create a new file. The request is `multipart/form-data` with `file` (the contents) and `path` (absolute path, including the file name). It never overwrites: use `PUT /files` to replace a file.

| Status | `code` | Meaning |
|--------|--------|---------|
| `201` | | Created; body is the File |
| `400` | `invalid_request` | Missing `file` or `path`, or a malformed `path` |
| `403` | `permission_denied` | The provider does not allow writing at `path` |
| `409` | `conflict` | A file or folder already exists at `path` |
| `422` | `invalid_operation` | A path the provider rejects, or a file over 150 MB |
| `502` / `503` | `provider_error` / `provider_unavailable` | The provider failed or is unreachable |

```bash
curl -X POST http://127.0.0.1:8000/files -F "file=@report.pdf" -F "path=/Docs/report.pdf"
```

### `PUT /files`
Replace the contents of an existing file. Same form fields as `POST /files`. It never creates a file, and the write is refused if the file changes while it is being replaced.

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Replaced; body is the File, which keeps its `id` |
| `400` | `invalid_request` | Missing `file` or `path`, or a malformed `path` |
| `403` | `permission_denied` | The provider does not allow writing at `path` |
| `404` | `not_found` | No file exists at `path` |
| `409` | `conflict` | The file changed during the replace; read it again and retry |
| `422` | `invalid_operation` | `path` is a folder, or the file is over 150 MB |
| `502` / `503` | `provider_error` / `provider_unavailable` | The provider failed or is unreachable |

```bash
curl -X PUT http://127.0.0.1:8000/files -F "file=@report.pdf" -F "path=/Docs/report.pdf"
```

### `POST /files/upload`
Upload a file, optionally overwriting (kept for existing callers; prefer `POST /files` and `PUT /files`). The request is `multipart/form-data`, not JSON.

- `file` (required): the file contents.
- `remote_path` (required): absolute destination path, including the file name.
- `overwrite` (optional, default `false`): replace an existing file at `remote_path`.

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Uploaded; body is the File. An overwritten file keeps its `id`. |
| `403` | `permission_denied` | The provider does not allow writing at `remote_path` (e.g. no write permission, or a team folder) |
| `409` | `conflict` | Something already exists at `remote_path` and `overwrite` is `false`, or a folder is there |
| `400` | `invalid_request` | Invalid form (relative or malformed `remote_path`, missing `file`) |
| `422` | `invalid_operation` | A path the provider rejects, or a file over 150 MB |
| `502` | `provider_error` | The provider failed (e.g. the account is out of space, or the service's Dropbox credentials have expired) |
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
| `200` | | Created; body is the folder (`size` and `modified_at` are `null`) |
| `403` | `permission_denied` | The provider does not allow creating a folder there |
| `409` | `conflict` | Something already exists at `remote_path` |
| `400` | `invalid_request` | Invalid body (malformed path, missing field) |
| `422` | `invalid_operation` | A path the provider rejects |
| `502` | `provider_error` | The provider failed (e.g. the account is out of space) |
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
| `400` | `invalid_request` | Invalid body (malformed path, missing field) |
| `422` | `invalid_operation` | `to_path` is inside `from_path` (a folder copied into itself) |
| `502` | `provider_error` | The provider failed (e.g. the account is out of space) |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl -X POST http://127.0.0.1:8000/files/copy \
  -H 'Content-Type: application/json' \
  -d '{"from_path": "/Docs", "to_path": "/Docs/Backup"}'
# 422 response body:
# {"error": {"code": "invalid_operation", "message": "Cannot copy '/Docs' into itself: '/Docs/Backup' is inside the source"}}
```

## Update endpoints

### `PATCH /files`
Move or rename a file: `PATCH /files?path=<from>` with a JSON body naming the full destination. A rename is a move within the same folder. As the shared conformance suite requires, only files move, and nothing at `new_path` is ever overwritten. The file keeps its `id`; missing parent folders of `new_path` are created.

```json
{"new_path": "/Archive/report.pdf"}
```

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Moved; body is the File at its new path |
| `400` | `invalid_request` | Missing or malformed `path` or `new_path` (including the root), or a body that is not JSON |
| `403` | `permission_denied` | The provider does not allow the move |
| `404` | `not_found` | Nothing exists at `path` |
| `409` | `conflict` | Something already exists at `new_path` |
| `422` | `invalid_operation` | `path` is a folder, or `new_path` is `path` itself (ignoring case) |
| `502` / `503` | `provider_error` / `provider_unavailable` | The provider failed or is unreachable |

```bash
curl -X PATCH 'http://127.0.0.1:8000/files?path=/Docs/report.pdf' \
  -H 'Content-Type: application/json' -d '{"new_path": "/Archive/report.pdf"}'
```

To move a folder, use `POST /files/move`.

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
| `400` | `invalid_request` | Invalid body (malformed path, empty name, name with `/`, missing field) |
| `502` | `provider_error` | The provider failed (e.g. the service's Dropbox credentials have expired) |
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
| `400` | `invalid_request` | Invalid body (malformed path, root as source, missing field) |
| `422` | `invalid_operation` | `to_path` is inside `from_path` (a folder moved into itself) |
| `502` | `provider_error` | The provider failed (e.g. the account is out of space, or the service's Dropbox credentials have expired) |
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
| `400` | `invalid_request` | Invalid query parameter (missing `path`, malformed or root path) |
| `502` | `provider_error` | The provider failed |
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
| `400` | `invalid_request` | Invalid query parameter (missing or malformed `path`) |
| `422` | `invalid_operation` | Path points to a folder |
| `502` | `provider_error` | The provider failed |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl -o downloaded.pdf "http://127.0.0.1:8000/files/download?path=/Docs/report.pdf"
```

### `GET /files`
List the direct children of the folder at `path`, sorted by path. Every provider page is fetched, so the whole folder comes back as one array with no cursor.

Query parameters:
- `path` (required): the folder to list (e.g. `?path=/Docs`); `""` or `/` lists the root.

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `200` | | Success; body is a JSON array of file info objects, each with `type` (`"file"` or `"folder"`) |
| `400` | `invalid_request` | Missing or malformed `path` |
| `403` | `permission_denied` | The provider does not allow listing the folder |
| `404` | `not_found` | Folder does not exist |
| `422` | `invalid_operation` | Path points to a file rather than a folder |
| `502` | `provider_error` | The provider failed |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl "http://127.0.0.1:8000/files?path=/Docs"
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
| `400` | `invalid_request` | Invalid query (`q` empty or over 1000 characters, or `max_results` not between 1 and 1000) |
| `502` | `provider_error` | The provider failed |
| `503` | `provider_unavailable` | The provider is unreachable or rate limited the request; try again later |

```bash
curl "http://127.0.0.1:8000/files/search?q=report&max_results=5"
```

## Delete endpoints

### `DELETE /files`
Delete a file, or a folder together with everything inside it.

Query parameters:
- `path` (required): the entry to delete (e.g. `?path=/Docs/report.pdf`). The root (`/` or empty) can never be deleted.

Errors use the [shared error format](#shared-error-format).

| Status | `code` | Meaning |
|--------|--------|---------|
| `204` | | Deleted; the response has no body |
| `403` | `permission_denied` | The provider does not allow deleting the entry (e.g. it is locked, a team folder, or read-only for this account) |
| `404` | `not_found` | Nothing exists at `path`, including a path that was already deleted |
| `400` | `invalid_request` | Missing or malformed `path` |
| `422` | `invalid_operation` | The path is the root, or a path the provider rejects as malformed |
| `502` | `provider_error` | The provider failed (e.g. the folder holds too many files to delete in one call, or the service's Dropbox credentials have expired) |
| `503` | `provider_unavailable` | The provider is unreachable or busy (e.g. too many write operations in progress); nothing was deleted, so try again later |

```bash
curl -X DELETE "http://127.0.0.1:8000/files?path=/Docs/report.pdf"
# 404 response body:
# {"error": {"code": "not_found", "message": "No file or folder at '/Docs/report.pdf'"}}
```

Delete is not idempotent: deleting the same path twice returns `204` the first time and `404` the second. The response has no body because the `Client.delete` contract returns only `True`, not the deleted entry. The root path is rejected by the client before the provider is contacted. Dropbox implements delete as a single `files_delete_v2` call; deleted entries go to the Dropbox account's trash, but this service does not offer restore.


## Testing
```bash
uv run pytest src/cloud_storage_service/tests/
```
Route tests replace the client with `app.dependency_overrides[get_storage_client]`, so they need no credentials or network. They use `FakeStorageClient` from [`tests/fakes.py`](../../tests/fakes.py), an in-memory `Client` you seed with `storage.add(path)` and then check through `storage.entries`. It implements only the operations some test needs; when you add routes for another operation, implement that method in the fake following the `Client` contract.
