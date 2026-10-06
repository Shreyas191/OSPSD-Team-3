# Cloud Storage Service

## Overview
`cloud_storage_service` is a FastAPI app that exposes the `cloud_storage_client_api.Client` contract over HTTP. Routes only talk to the abstract `Client`; importing the package registers the Dropbox implementation, so a running service acts on the Dropbox account configured in `.env`.

## Running locally
```bash
uv run uvicorn cloud_storage_service.app:app --reload
```
Interactive docs (Swagger UI) are served at `http://127.0.0.1:8000/docs`.

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
Route tests override `get_storage_client` with a mock, so they need no credentials.
