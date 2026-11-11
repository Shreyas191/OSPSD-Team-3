# Tutorial: Using the Cloud Storage Service

This walks through every operation of the service against a real Dropbox app folder, using the vertical's shared routes. Each step gives the request, what we expect, and what actually came back.

The steps run in order and build on each other. They work inside one scratch folder, `/ospsd-tutorial`, which the last step deletes, so the app folder ends up unchanged.

| Section | Operations | Presented by |
|---|---|---|
| [Create](#1-create) | create, replace, create folder, copy | Zesan Rahman |
| [Read](#2-read) | metadata, download, list, search | Jing Qian |
| [Update](#3-update) | move or rename | Shreyas Kaldate |
| [Delete](#4-delete) | delete | Ashik John |

The outputs below come from one run on **November 11, 2026, against the deployed test service** (release `7d6b6ff`). IDs, sizes, timestamps and `etag` values will differ in your run; status codes and error bodies should match. The October 21 version of this tutorial used the HW1 routes. Its Swagger UI screenshots showed routes that no longer exist (`/folders/list`) or are now Team 3 extensions, so this version shows commands and their actual output instead. Swagger UI at `/docs` still works for every step: expand the operation, click **Try it out**, enter the same values, and click **Execute**.

## 0. Setup

Run the service locally against your own Dropbox app:

1. Follow the [README setup](README.md#project-setup): install `uv`, run `uv sync --all-packages --extra dev`, create `.env`, and log in to Dropbox once with `uv run python -m dropbox_client_impl login`.
2. Start the service with `uv run python main.py`.
3. In another terminal:

   ```bash
   B=http://127.0.0.1:8000
   T=/ospsd-tutorial
   printf 'Q3 report, draft 1\n'       > report.txt      # 19 bytes
   printf 'Q3 report, final version\n' > report-v2.txt   # 25 bytes
   ```

To use the deployed test service instead, set `B=https://cloud-storage-service-test-gyesjhieva-uc.a.run.app`. It writes to the test environment's own Dropbox app, not yours.

### How errors look

Every failure uses the same body, whichever operation it comes from:

```json
{"error": {"code": "<code>", "message": "<what went wrong>"}}
```

| Status | `code` | Meaning |
|---|---|---|
| `400` | `invalid_request` | The request is malformed (bad path, missing field) |
| `403` | `permission_denied` | Dropbox doesn't allow the operation on this entry |
| `404` | `not_found` | Nothing exists at the path |
| `404` | `route_not_found` | No such route |
| `405` | `method_not_allowed` | The route doesn't accept this method |
| `409` | `conflict` | The destination is taken, or the file changed during a replace |
| `413` | `file_too_large` | The upload is over Dropbox's limit |
| `422` | `invalid_operation` | Well-formed, but not allowed (download a folder, move a folder with `PATCH`, delete the root, …) |
| `500` | `internal_error` | A bug in the service |
| `502` | `provider_error` | Dropbox failed; details are logged, not returned |
| `503` | `provider_unavailable`, `service_busy` | Dropbox is unreachable, or the service is at its limit; try again later |

Branch on the status and `code`, never on the message.

---

## 1. Create

*Presented by Zesan Rahman.* Create a file, replace it, and see that neither one ever does the other's job.

### 1.1 Create a new file → `201`

```bash
curl -s -X POST "$B/files" -F file=@report.txt -F path=$T/Docs/report.txt
```

- **Expected:** `201` and the new file's info. The missing folders `/ospsd-tutorial/Docs` are created automatically.
- **Actual:** `201`

  ```json
  {"id":"id:KzOOg5ouvzUAAAAAAAABlQ","name":"report.txt","path":"/ospsd-tutorial/Docs/report.txt","size":19,"modified_at":"2026-11-11T22:39:13Z","etag":"0166007dd41306800000003923a8b33"}
  ```

### 1.2 Create it again → `409`

Same request with `report-v2.txt`.

- **Expected:** `409 conflict`. Create never overwrites.
- **Actual:** `409`, `{"error":{"code":"conflict","message":"Something already exists at '/ospsd-tutorial/Docs/report.txt'"}}`

### 1.3 Replace it → `200`

```bash
curl -s -X PUT "$B/files" -F file=@report-v2.txt -F path=$T/Docs/report.txt
```

- **Expected:** `200`. The content is new (25 bytes), the `id` is the same, and the `etag` changes.
- **Actual:** `200`, `"size":25`, the same `"id":"id:KzOOg5ouvzUAAAAAAAABlQ"`, and `"etag":"0166007dd5f13c300000003923a8b33"`.

If someone else changes the file while it is being replaced, the service answers `409 conflict` and keeps their version. Read the file again and retry.

### 1.4 Replace a file that doesn't exist → `404`

```bash
curl -s -X PUT "$B/files" -F file=@report-v2.txt -F path=$T/Docs/missing.txt
```

- **Expected:** `404 not_found`. Replace never creates a file.
- **Actual:** `404`, `"No file or folder at '/ospsd-tutorial/Docs/missing.txt'"`

### 1.5 Create a folder → `200` (Team 3 extension)

```bash
curl -s -X POST "$B/folders/create" -H 'content-type: application/json' -d "{\"remote_path\":\"$T/Archive\"}"
```

- **Expected:** `200`. A folder has `size`, `modified_at` and `etag` set to `null`.
- **Actual:** `200`, `{"id":"id:KzOOg5ouvzUAAAAAAAABmA","name":"Archive","path":"/ospsd-tutorial/Archive","size":null,"modified_at":null,"etag":null}`

### 1.6 Copy a file → `200` (Team 3 extension)

```bash
curl -s -X POST "$B/files/copy" -H 'content-type: application/json' \
  -d "{\"from_path\":\"$T/Docs/report.txt\",\"to_path\":\"$T/Archive/report-copy.txt\"}"
```

- **Expected:** `200`. The copy is a new entry with its own `id`, and the source stays in place.
- **Actual:** `200`, `"path":"/ospsd-tutorial/Archive/report-copy.txt"`, `"id":"id:KzOOg5ouvzUAAAAAAAABmQ"`, which differs from the source's.

---

## 2. Read

*Presented by Jing Qian.* Nothing here changes Dropbox. At this point `/ospsd-tutorial/Docs/report.txt` (25 bytes) and `/ospsd-tutorial/Archive/report-copy.txt` exist.

### 2.1 Get a file's metadata → `200`

```bash
curl -s "$B/files/metadata?path=$T/Docs/report.txt"
```

- **Expected:** `200`, with the same `id`, size and `etag` as after the replace in 1.3.
- **Actual:** `200`, `{"id":"id:KzOOg5ouvzUAAAAAAAABlQ","name":"report.txt","path":"/ospsd-tutorial/Docs/report.txt","size":25,"modified_at":"2026-11-11T22:39:15Z","etag":"0166007dd5f13c300000003923a8b33"}`

### 2.2 Metadata for a path that doesn't exist → `404`

- **Expected:** `404 not_found`.
- **Actual:** `404`, `"No file or folder at '/ospsd-tutorial/Docs/missing.txt'"`

### 2.3 Download a file and compare its bytes → `200`

```bash
curl -s -o downloaded.txt "$B/files/download?path=$T/Docs/report.txt"
cmp report-v2.txt downloaded.txt && echo identical
```

- **Expected:** `200`, with exactly the bytes of `report-v2.txt`.
- **Actual:** `200`, `identical`.

### 2.4 Download a folder → `422`

- **Expected:** `422 invalid_operation`. Only files can be downloaded.
- **Actual:** `422`, `"Path is a folder: '/ospsd-tutorial/Docs'"`

### 2.5 List a folder → `200`

```bash
curl -s "$B/files?path=$T"
```

- **Expected:** `200`, with the direct children sorted by path. Each entry has a `type`.
- **Actual:** `200`

  ```json
  [{"id":"id:KzOOg5ouvzUAAAAAAAABmA","name":"Archive","path":"/ospsd-tutorial/Archive","size":null,"modified_at":null,"etag":null,"type":"folder"},
   {"id":"id:KzOOg5ouvzUAAAAAAAABlw","name":"Docs","path":"/ospsd-tutorial/Docs","size":null,"modified_at":null,"etag":null,"type":"folder"}]
  ```

### 2.6 List a file → `422`

- **Expected:** `422 invalid_operation`. Only folders can be listed.
- **Actual:** `422`, `"Path is a file: '/ospsd-tutorial/Docs/report.txt'"`

### 2.7 Search by name → `200` (Team 3 extension)

```bash
curl -s "$B/files/search?q=report-copy&max_results=5"
```

- **Expected:** `200`, with at most 5 matches from the whole app folder.
- **Actual:** `200`, one result: `"path":"/ospsd-tutorial/Archive/report-copy.txt"`.

---

## 3. Update

*Presented by Shreyas Kaldate.* `PATCH /files` moves or renames a file. It never creates an entry or overwrites one.

### 3.1 Move and rename a file → `200`

```bash
curl -s -X PATCH "$B/files?path=$T/Docs/report.txt" -H 'content-type: application/json' \
  -d "{\"new_path\":\"$T/Archive/summary.txt\"}"
```

- **Expected:** `200`. It is the same file with the same `id`, at a new path.
- **Actual:** `200`, `"path":"/ospsd-tutorial/Archive/summary.txt"`, the same `"id":"id:KzOOg5ouvzUAAAAAAAABlQ"`. The `etag` changed as well (`0166007ddc12d4e00000003923a8b33`): Dropbox gives a moved file a new revision.

### 3.2 Repeat the same move → `404`

- **Expected:** `404 not_found`, since nothing is left at the source. This is why retrying a move is safe: a repeat never moves anything twice.
- **Actual:** `404`, `"No file or folder at '/ospsd-tutorial/Docs/report.txt'"`

### 3.3 Move onto a name that is taken → `409`

`PATCH /files?path=/ospsd-tutorial/Archive/summary.txt` with `{"new_path": "/ospsd-tutorial/Archive/report-copy.txt"}`.

- **Expected:** `409 conflict`. Neither file changes.
- **Actual:** `409`, `"Something already exists at '/ospsd-tutorial/Archive/report-copy.txt'"`

### 3.4 Move a folder → `422`

`PATCH /files?path=/ospsd-tutorial/Archive` with `{"new_path": "/ospsd-tutorial/Old"}`.

- **Expected:** `422 invalid_operation`. The shared contract's `PATCH` moves files only.
- **Actual:** `422`, `"Cannot move '/ospsd-tutorial/Archive': it is a folder, and only files can be moved"`

To move a folder on our service, use the Team 3 extension `POST /files/move` with `{"from_path", "to_path"}`.

### 3.5 Change only the letter case → `422`

`PATCH /files?path=/ospsd-tutorial/Archive/summary.txt` with `{"new_path": "/ospsd-tutorial/Archive/Summary.txt"}`.

- **Expected:** `422 invalid_operation`. Dropbox paths ignore case, so this is the same path.
- **Actual:** `422`, `"Cannot move '/ospsd-tutorial/Archive/summary.txt' to '/ospsd-tutorial/Archive/Summary.txt': it is the same path (ignoring case)"`

---

## 4. Delete

*Presented by Ashik John.* Deleted entries go to the Dropbox app's deleted files, where Dropbox keeps them restorable for 30 days.

### 4.1 Delete a file → `204`

```bash
curl -s -i -X DELETE "$B/files?path=$T/Archive/report-copy.txt"
```

- **Expected:** `204` with no body.
- **Actual:** `204`.

### 4.2 Delete it again → `404`

- **Expected:** `404 not_found`. Delete is not idempotent.
- **Actual:** `404`, `"No file or folder at '/ospsd-tutorial/Archive/report-copy.txt'"`

### 4.3 Delete the root → `422`

```bash
curl -s -X DELETE "$B/files?path=/"
```

- **Expected:** `422 invalid_operation`. The root is a valid path, but it is never deleted, and Dropbox isn't called.
- **Actual:** `422`, `"Refusing to delete the root folder (path '/')"`

### 4.4 A path with `..` → `400`

```bash
curl -s -X DELETE "$B/files?path=$T/../Docs"
```

- **Expected:** `400 invalid_request`. Empty, `.` and `..` segments are malformed, and Dropbox isn't called.
- **Actual:** `400`, `"query.path: Value error, path must not contain empty, '.' or '..' segments"`

A path without the leading `/` is accepted: `GET /files/metadata?path=ospsd-tutorial/Archive/summary.txt` returned `200` with `"path":"/ospsd-tutorial/Archive/summary.txt"`.

---

## 5. Mistakes that never reach a route

These come from the app's own error handlers, in the same body as every other error:

| Request | Status | Body |
|---|---|---|
| `GET /folders/list?path=/ospsd-tutorial` (the HW1 list route, now removed) | `404` | `{"error":{"code":"route_not_found","message":"No route /folders/list"}}` |
| `POST /files/metadata?path=/ospsd-tutorial` | `405` | `{"error":{"code":"method_not_allowed","message":"POST is not allowed on /files/metadata"}}` |

## 6. Health and logs

`GET /health` answered `{"status":"ok"}` without calling Dropbox. `GET /health/provider` answered `{"status":"ok"}`, so Dropbox accepted the service's credentials.

The service writes one JSON log line per request. Locally they print in the terminal running `main.py`; on Cloud Run they are in Cloud Logging (see [Operations](docs/operations.md#4-where-are-its-logs)). These are the deployed service's actual lines for steps 1.1 and 2.2, with some fields left out (`logger`, `message`, `route`, `service`, `span_id`):

```json
{"event": "request", "operation": "create_file", "method": "POST", "status": 201, "outcome": "ok", "duration_ms": 1006, "release": "7d6b6ff356e3fba5189392643d409c8b257563d6", "trace_id": "fe3250027cdcd8f37ed5b99ff8b6387a"}
{"event": "request", "operation": "get_file_metadata", "method": "GET", "status": 404, "outcome": "not_found", "code": "not_found", "provider_failure": false, "reason": "No file or folder at '…'", "duration_ms": 118.1, "release": "7d6b6ff356e3fba5189392643d409c8b257563d6", "trace_id": "e7f6780d6cffa668413f019c9ba46823"}
```

Each line names the operation, status, outcome, whether Dropbox caused a failure (`provider_failure`), and the release that served it. File paths are replaced with `…`, and logs never include tokens, file contents or the values a caller sent that failed validation.

## 7. Clean up

```bash
curl -s -i -X DELETE "$B/files?path=$T"
curl -s "$B/files?path=$T"
```

- **Expected:** `204`, then `404`.
- **Actual:** `204`, then `{"error":{"code":"not_found","message":"No file or folder at '/ospsd-tutorial'"}}`.

To disconnect this machine from Dropbox afterwards, run `uv run python -m dropbox_client_impl logout`.
