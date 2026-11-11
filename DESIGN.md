# Team 3 Design Document

**By:** John Ashik, Shreyas Kaldate, Zesan Rahman, Jing Qian, Victor Wang
**Vertical:** Document Storage (provider: Dropbox)
**Reviewers:** Victor Wang (Team 3, whole design), Darsh (Team 6, contract-facing parts)
**Last updated:** 11/11/2026
**Status:** Implemented on `hw2` and deployed to the test service (release `7d6b6ff`). The design review in [Review](#review) is not complete. Sections written on Nov 4 as a plan now describe what was built. [What changed during implementation, and why](#what-changed-during-implementation-and-why) lists every place where the build differs from the Nov 4 plan.

**Links:**

- Shared contract: [Dayana20/ospsd26-cloud-storage](https://github.com/Dayana20/ospsd26-cloud-storage), latest tag [`0.3.1`](https://github.com/Dayana20/ospsd26-cloud-storage/tree/0.3.1). Its package `cloud_storage_api` defines the shared models and error table.
- Shared conformance suite: Team 6's commit [`b77617c`](https://github.com/prateeks99/ospsd26-cloud-storage/tree/b77617c218d345c8cde28dbf08cf4d1a5cbaef38), pinned in CI until the contract repository merges it (its PR #6).
- Decision memo: <https://docs.google.com/document/d/1K9U8uO-FVbeu0PGlWpr_frJ6YQmoHXdz/edit?usp=sharing&ouid=104071874439967208816&rtpof=true&sd=true>
- Team 3 repository: <https://github.com/Shreyas191/OSPSD-Team-3> (branch `hw2`)
- Operating the deployed service (what runs, which version, health, logs, rollback): [docs/operations.md](docs/operations.md)

## Introduction

The Document Storage vertical provides one HTTP contract for consumers that need to store and manage files across the Team 1, Team 3 and Team 6 services. Consumers should be able to use the same request formats and interpret responses the same way, even though each service uses a different storage provider.

### Context

The three services support seven overlapping operations: upload, replace, move, list, download, metadata and delete. Before this work, their APIs differed in ways that forced consumers to write provider-specific code. For example, uploading a file was `POST /files` with `file` and `path` on Team 1, `POST /files/upload` with `file` and `remote_path` on Team 3, and `POST /files/{key}` on Team 6. Error bodies and metadata fields also differed. Some differences changed behaviour: Team 3 deleted a folder and its contents, while Team 1 rejected folder deletion. A consumer that switched services had to change its requests and could get different behaviour for the same operation. A consumer that used several services had to track how each one worked.

### Goals and non-goals

Goals, in no particular order:

- Give each common operation one agreed route, HTTP method and request shape across all participating services.
- Return a consistent file information object from upload, metadata lookup, list and move.
- Use one shared error response format, with consistent meanings for HTTP statuses and error codes, so consumers can handle errors without relying on provider-specific messages.
- Make operations whose effects could differ, such as creating versus replacing a file or deleting a folder, behave as one documented contract says.
- Let a consumer's common workflow run against each participating service with the same requests and outcome handling, changing only the base URL, credentials and provider-specific identifiers.
- Follow RESTful practice in every route, so consumers can predict them.

Non-goals, which a reader might expect but we are not pursuing:

- Migrating existing files, folders or identifiers between providers.
- Exposing every capability of each storage provider through the shared contract.
- Switching storage providers during an in-progress request.
- Standardizing authentication and credential provisioning. Each service keeps its own credentials and setup, and consumers provide what the service they call requires.

## What changed during implementation, and why

The Nov 4 version of this document was a plan. This table records each place where the implementation differs from it, as of release `7d6b6ff` (Nov 11). Every other section has been updated to match the code.

| # | Nov 4 plan | What was built | Why | Where |
|---|---|---|---|---|
| 1 | Replace every HW1 route; no aliases (Decision 2) | `POST /files/upload`, `POST /files/move`, `POST /files/rename`, `POST /files/copy`, `POST /folders/create` and `GET /files/search` stay as Team 3 extensions. Only `GET /folders/list` was removed. Bad input is `400` on every route, old and new. | Removing upload and move would have broken our own tests, tutorial and e2e checks in the same PRs that added the shared routes. Keeping them costs one handler each, and they share the new routes' validation, error body and logs. The worry behind Decision 2, that an alias would answer the same mistake two ways, didn't happen: the app-level validation handler gives every route `400 invalid_request`. | `edbdd8a`, `f670154` |
| 2 | `PATCH /files` moves files and folders, and refuses case-only renames | `PATCH /files` moves **files only**. A folder, or a `new_path` equal to `path` ignoring case, is `422 invalid_operation`. `POST /files/move` still moves folders. | The shared conformance suite expects these rules. Applying them needs the source's metadata first, so a `PATCH` makes one extra Dropbox call. | `2f09cf7` |
| 3 | Optional `etag` (planned); uploads over 150 MiB are `400 invalid_request` | Every file response has `etag`, set to the Dropbox `rev` (`null` for folders). An upload Dropbox refuses as too large is `413 file_too_large`. | Contract `0.3.1` makes `etag` part of `FileInfo` and adds `413 file_too_large`. Dropbox documents its limit as 150 MB. | `d5a5c32` |
| 4 | Not anticipated | On Cloud Run, uploads above about 32 MiB never reach the service. Google's front end answers `413` with an HTML page, not the shared error body. | Measured on Nov 11: a 31 MiB upload returned `201`, and a 33 MiB upload returned `413 text/html`. Cloud Run limits HTTP/1 request bodies. | [Failure behavior](#failure-behavior), open question 13 |
| 5 | Depend on the shared Python package once it is tagged; remove `src/cloud_storage_client_api/` | The service still uses our local `cloud_storage_client_api` package. Contract `0.3.1` exists but is not pinned. | `0.3.1` defines a different interface (`DocumentStorage` with `create`, `replace`, `list_files` and `move`), not a drop-in for our `Client`. The HTTP behaviour already matches the contract and passes the conformance suite. Switching the Python dependency is a separate change. | Open question 1 |
| 6 | Python 3.11 → 3.12 once the contract is tagged | Done: Python 3.12 everywhere, including the container image and CI. | The contract package needs 3.12. | `pyproject.toml`, `.python-version` |
| 7 | Telemetry: OpenTelemetry to Grafana Cloud; alerts to Slack | Google Cloud Logging, Trace and Monitoring in the same project, with one dashboard and one alert, emailed to the team. | Item 3.1 sent the data to Google Cloud's own services. Grafana would need a second account and a Google service-account key to read them, plus one more thing to own and tear down, for the same charts. The handout leaves the backend to the team. | [Dashboard](docs/dashboard.md) |
| 8 | Conformance suite owned by Team 6, run before each release | It isn't merged in the contract repository yet, so CI pins Team 6's commit `b77617c` and runs it on every push against our real app on an in-memory client. Every deploy also runs the shared consumer workflow against the new revision, with real Dropbox, before it gets traffic. | It was the only available version of the suite. | [Testing](docs/testing.md) |
| 9 | Not planned | Fault injection (`FAULT_INJECTION`) wraps the client on the test service only. The service refuses to start if it is set on demo. | Levels 3.3 and 3.6 need repeatable provider failures. | [Fault Injection](docs/fault-injection.md) |
| 10 | Deploy to test and demo | Every push to `hw2` deploys test, then demo, and demo only after the same release passed its smoke check on test. `main` keeps the same pipeline but isn't updated for HW2. | `hw2` is the branch the team submits for HW2, so it ships to both environments. | [Release Pipeline](docs/release-pipeline.md) |
| 11 | Open questions 2, 3, 5, 9, 10 and 11 | Settled: see [Open questions](#open-questions). Stale replace and occupied destinations are `409 conflict`. Codes follow the contract's table. A partial folder delete is `502 provider_error`. Dropbox's stale-rev error was observed live. | Contract `0.3.1` and a live test (Nov 8). | — |

## Architecture as built

**Figure 1. How a consumer's request reaches Dropbox, and which package owns each step.** Solid arrows are calls at request time. The `◄──` arrow means "implements": `dropbox_client_impl` implements the `Client` contract. The service never imports `dropbox`.

```text
                    ┌──────────────────────────┐
                    │   HTTP client / caller   │
                    └────────────┬─────────────┘
                                 │ HTTPS (REST, ?path=)
                                 ▼
                    ┌──────────────────────────┐
                    │ Cloud Run (us-central1)  │  test: every push to hw2
                    │ ≤ 20 requests/instance,  │  demo: after test passes
                    │ ≤ 2 instances            │
                    └────────────┬─────────────┘
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ cloud_storage_service                                              │
│   ├── RequestTelemetryMiddleware  (one JSON log line + trace span) │
│   ├── ConcurrencyLimitMiddleware  (≤ 10 in flight, else 503)       │
│   ├── App-level exception handlers (400, 404, 405, 500 bodies)     │
│   ├── FastAPI routes               (HTTP -> Client contract calls) │
│   └── SharedErrorRoute             (exception -> status and code)  │
└────────────┬──────────────────────────────────┬────────────────────┘
             │ calls the abstract contract      │ builds at startup
             ▼                                  ▼  (wrapped by faults.py
┌──────────────────────────────────┐   ┌──────────────────────┐ on test only,
│ cloud_storage_client_api         │◄──│ dropbox_client_impl  │ when FAULT_INJECTION
│   (Client, File, get_client())   │   │   (DropboxClient)    │ is set)
└──────────────────────────────────┘   └──────────┬───────────┘
                                                  │ SDK calls (traced)
                                                  ▼
                                       ┌──────────────────────┐
                                       │ Dropbox API          │
                                       │ (one app per env.)   │
                                       └──────────────────────┘
```

**Responsibilities and dependency direction.**

| Component | Owns | Depends on |
|---|---|---|
| `cloud_storage_service` | HTTP routes, request validation, overload protection, the shared error body, request logs and traces | The `Client` contract only |
| `cloud_storage_client_api` (local; see change 5) | The abstract `Client` and `File` classes, `FileTooLargeError` and `get_client()` | Nothing |
| `dropbox_client_impl` | `DropboxClient`, `DropboxFile`, OAuth in `auth.py`, and the translation of Dropbox errors to built-in exceptions in `errors.py` | The contract and the Dropbox SDK |
| `infra/` (OpenTofu) | Cloud Run services, Artifact Registry, secrets, service accounts, CI sign-in, log-based metrics, dashboard and alert | Google Cloud |

**Who creates and closes the provider client.** The app's `lifespan` calls `build_storage_client()` once at startup, wraps it in fault injection if `FAULT_INJECTION` is set, and stores it in `app.state.storage_client`. Startup fails, with one sanitized line, if required configuration is missing. Routes get the client through the `get_storage_client` dependency, so tests can swap in `FakeStorageClient`. The client lives as long as the process and is not closed explicitly; the Dropbox SDK's HTTP session ends when the process exits.

### Guarantees each request gets

- **Paths:** a path may omit the leading `/`; the service adds it. Empty segments, a trailing `/`, and `.` or `..` segments are `400 invalid_request`. Deleting the root (`?path=/`) is `422 invalid_operation`. Dropbox compares paths ignoring case.
- **Status boundaries:**
  - `400 invalid_request`: malformed input (a bad path, or a missing field or body).
  - `422 invalid_operation`: well-formed but not allowed, such as moving a folder with `PATCH`, downloading a folder, listing a file, or deleting the root.
  - `404 not_found`: the target doesn't exist. An unknown route is `404 route_not_found`, so a typo is never read as a missing file.
  - `409 conflict`: the destination is occupied, or the file changed during a replace.
  - `413 file_too_large`, `403 permission_denied`, `502 provider_error`, and `503 provider_unavailable` / `service_busy`.
- **File info:** `id`, `name`, `path`, `size` (`null` for folders), `modified_at` (UTC, ending in `Z`; `null` for folders) and `etag` (`null` for folders). List entries add `type` (`file` or `folder`).
- **Health:** `GET /health` answers without calling Dropbox, so Cloud Run's liveness probe never restarts the service because Dropbox is down. `GET /health/provider` makes one `users_get_current_account()` call.
- **Telemetry:** one JSON log line per request (operation, status, outcome, duration, release, trace ID) and a trace with a child span for each Dropbox HTTP call. File paths, query strings, bodies and headers are never logged; quoted text in error reasons is redacted.

### Request flow

Both traces below are real: requests to the test service on Nov 11, 2026, release `7d6b6ff`.

**A successful request: `GET /files/metadata?path=/ospsd-tutorial/Docs/report.txt`**

1. `RequestTelemetryMiddleware` starts a span and a timer. `ConcurrencyLimitMiddleware` admits the request (fewer than 10 in flight).
2. FastAPI matches `get_file_metadata` and validates `path` with `clean_path`.
3. The `StorageClient` dependency returns `app.state.storage_client`.
4. The route calls `client.get_metadata("/ospsd-tutorial/Docs/report.txt")`.
5. `DropboxClient` calls `files_get_metadata`, which is traced as a child span, and wraps the `FileMetadata` in a `DropboxFile`.
6. `FileResponse.from_file(file)` builds the body, and FastAPI answers `200`:

   ```json
   {"id": "id:KzOOg5ouvzUAAAAAAAABlQ", "name": "report.txt", "path": "/ospsd-tutorial/Docs/report.txt",
    "size": 25, "modified_at": "2026-11-11T22:39:15Z", "etag": "0166007dd5f13c300000003923a8b33"}
   ```

7. The middleware writes one log line: `{"operation": "get_file_metadata", "status": 200, "outcome": "ok", "duration_ms": 143.8, "release": "7d6b6ff…", "trace_id": "e79f34c1a428b59b39e4df6db476e7b2", …}`.

**An expected failure: `GET /files/metadata?path=/ospsd-tutorial/Docs/missing.txt` → 404**

1. Steps 1 to 4 as above.
2. `files_get_metadata` raises `ApiError` with `path/not_found`.
3. `dropbox_client_impl.errors.translate` raises the built-in `FileNotFoundError("No file or folder at '…'")`. No Dropbox type leaves the adapter.
4. `SharedErrorRoute` maps `FileNotFoundError` to `404 not_found` and records the failure for the request log.
5. Response:

   ```json
   {"error": {"code": "not_found", "message": "No file or folder at '/ospsd-tutorial/Docs/missing.txt'"}}
   ```

6. Log line: `"outcome": "not_found"`, `"provider_failure": false`, `"reason": "No file or folder at '…'"`, `"duration_ms": 118.1`. The path is redacted.

## API and data model

**Contract source.** Routes, request shapes, the file-info fields and the error table follow contract tag `0.3.1` and the shared conformance suite at `b77617c`. The suite passes: 55 tests in CI on every push, and against the deployed test service by hand on Nov 11 ([Testing](docs/testing.md)).

### Shared operations

| Operation | Route | Request | Success |
|---|---|---|---|
| Health | `GET /health`, `GET /health/provider` | none | `200 {"status":"ok"}` |
| Create | `POST /files` | multipart: `file`, `path` | `201` file info |
| Replace | `PUT /files` | multipart: `file`, `path` | `200` file info |
| Metadata | `GET /files/metadata?path=` | `path` | `200` file info |
| Download | `GET /files/download?path=` | `path` | `200` raw bytes |
| List | `GET /files?path=` | folder `path` (default `/`) | `200` array, sorted by path, each with `type` |
| Move or rename a file | `PATCH /files?path=<from>` | JSON `{"new_path": "<to>"}` | `200` file info |
| Delete | `DELETE /files?path=` | `path` | `204` |

### Team 3 extensions

These aren't part of the shared contract, and other teams' services may not have them. They use the same validation, error body and logs as the shared routes.

| Route | Request | Success | Note |
|---|---|---|---|
| `POST /files/upload` | multipart: `file`, `remote_path`, `overwrite` | `200` file info | HW1's upload. With `overwrite=true` it creates or overwrites in one Dropbox call, with no revision check. |
| `POST /files/move` | JSON `from_path`, `to_path` | `200` file info | Moves folders too, unlike `PATCH /files` |
| `POST /files/rename` | JSON `path`, `new_name` | `200` file info | Stays in the same folder |
| `POST /files/copy` | JSON `from_path`, `to_path` | `200` file info | The copy gets a new `id` |
| `POST /folders/create` | JSON `remote_path` | `200` file info | |
| `GET /files/search?q=&max_results=` | query | `200` array | Matches names across the whole app folder |

### File-info object

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Opaque Dropbox ID. It stays the same across move, rename and replace; a copy gets a new one. Unique only within one Dropbox app folder. |
| `name` | string | The last path segment |
| `path` | string | Canonical path starting with `/` |
| `size` | integer or null | Bytes; `null` for folders |
| `modified_at` | string or null | ISO 8601 UTC ending in `Z`; `null` for folders |
| `etag` | string or null | The Dropbox `rev`. It changes whenever the file changes, including on a move (observed). `null` for folders. |
| `type` | `file` or `folder` | List entries only |

### Behaviour

- **Create** never overwrites anything, not even identical bytes (Dropbox add mode with `strict_conflict`).
- **Replace** returns `404` for a missing file and never creates one. It is refused with `409` if the file changes between our two Dropbox calls (Decision 1).
- **Move** (`PATCH`) moves only files and never overwrites an existing destination.
- **Delete** also removes a folder's contents. Deleting again returns `404`.
- **List** is one level deep. The service fetches every Dropbox page, so consumers never handle cursors.

### Errors

All errors use `{"error": {"code", "message"}}`. Consumers should branch on the status and code, not on the message.

| Status | `code` | When |
|---|---|---|
| `400` | `invalid_request` | Malformed request: bad path, or a missing field or body |
| `403` | `permission_denied` | Dropbox refused the operation on this entry |
| `404` | `not_found` | Nothing at the path |
| `404` | `route_not_found` | No such route |
| `405` | `method_not_allowed` | The route exists, but not with this method (the `Allow` header is kept) |
| `409` | `conflict` | Destination occupied, or the file changed during a replace |
| `413` | `file_too_large` | Dropbox refused the upload's size (see [Failure behavior](#failure-behavior) for Cloud Run's lower limit) |
| `422` | `invalid_operation` | Not allowed: folder moved with `PATCH`, folder downloaded, file listed, root deleted, same path ignoring case |
| `500` | `internal_error` | A bug in the service |
| `502` | `provider_error` | Dropbox failed, including a partial folder delete |
| `503` | `provider_unavailable` | Dropbox unreachable or rate limiting us |
| `503` | `service_busy` | More than 10 requests in flight on this instance |

After a `502` or `503` on a write, the outcome is uncertain: check metadata before retrying.

### Examples (observed on the test service, Nov 11, 2026, release `7d6b6ff`)

```http
POST /files            (multipart: file=report.txt, path=/ospsd-tutorial/Docs/report.txt)

201 Created
{"id":"id:KzOOg5ouvzUAAAAAAAABlQ","name":"report.txt","path":"/ospsd-tutorial/Docs/report.txt","size":19,"modified_at":"2026-11-11T22:39:13Z","etag":"0166007dd41306800000003923a8b33"}
```

```http
PATCH /files?path=/ospsd-tutorial/Docs/report.txt
{"new_path": "/ospsd-tutorial/Archive/summary.txt"}

200 OK
{"id":"id:KzOOg5ouvzUAAAAAAAABlQ","name":"summary.txt","path":"/ospsd-tutorial/Archive/summary.txt","size":25,"modified_at":"2026-11-11T22:39:21Z","etag":"0166007ddc12d4e00000003923a8b33"}
```

```http
PATCH /files?path=/ospsd-tutorial/Archive
{"new_path": "/ospsd-tutorial/Old"}

422 Unprocessable Content
{"error":{"code":"invalid_operation","message":"Cannot move '/ospsd-tutorial/Archive': it is a folder, and only files can be moved"}}
```

```http
GET /folders/list?path=/ospsd-tutorial

404 Not Found
{"error":{"code":"route_not_found","message":"No route /folders/list"}}
```

The full run, with every shared operation and its failures, is in [`TUTORIAL.md`](TUTORIAL.md).

### What changed from HW1 for callers

- **Upload:** `POST /files` (create, `201`) and `PUT /files` (replace, `200`) are the shared routes. `POST /files/upload` still works.
- **Move:** `PATCH /files?path=` with `{"new_path"}` is the shared route, for files only. `POST /files/move` still works and still moves folders.
- **List:** `GET /folders/list` is gone, and calling it returns `404 route_not_found`. Use `GET /files?path=`, which adds `type` and sorts by path.
- **File info:** `is_folder` is dropped, `modified` is renamed `modified_at` (UTC with `Z`), and `etag` is added.
- **Errors:** validation failures are `400 invalid_request`, not `422 invalid_input`. Provider failures are `502 provider_error`, not `provider_failure`. Unknown routes, wrong methods and unexpected errors use the shared body.
- **New:** `GET /health` and `GET /health/provider`.

**Information lost:** metadata no longer says whether a path is a folder (a list entry's `type` does). Replace takes 2 Dropbox calls, not 1, and `PATCH` takes 2. Case-only renames through `PATCH` are refused.

## Alternatives

Three decisions, each compared with its alternatives. Decision 1 matters most to consumers, because the wrong choice loses data without an error. Each now has an **Outcome**.

### Decision 1: How `PUT /files` replaces a file

The memo says replace returns `404 not_found` for a missing file and never creates one. It also plans a revision check, so that a concurrent change is refused instead of overwritten.

| Option | Dropbox calls | Missing file | File changed by someone else |
|---|---|---|---|
| A. `files_upload` with overwrite | 1 | Created, which breaks the 404 rule | Overwritten without an error |
| B. `files_get_metadata`, then `files_upload` with overwrite | 2 | `404 not_found` | Overwritten if it changes between our two calls |
| C. `files_get_metadata`, then `files_upload` with `update(rev)` (**chosen**) | 2 | `404 not_found` | Refused with `409` if it changes between our two calls |
| D. The consumer sends the revision it last read, in an `If-Match` header | 1 | `404 not_found` | Refused if it changed since the consumer read it |

**Evidence.** Dropbox's [upload documentation](https://www.dropbox.com/developers/documentation/http/documentation#files-upload) says update mode overwrites only "if the given rev matches the existing file's rev". The 404 rule already needs the metadata call, so passing its rev to the upload adds no call.

**Outcome: implemented.** The live test `test_replace_against_stale_revision_is_a_conflict` (Nov 8) observed Dropbox's answer to a stale rev with `autorename=False`: `ApiError` with `path/conflict/file`. The file keeps the newer content, and replace raises `FileExistsError`, which is `409 conflict`. `test_replace_missing_file_raises_and_creates_nothing` checks the 404 rule. One gap remains, an accepted risk: a file deleted by someone else *between* our two calls is recreated, because Dropbox always writes to a path with no file. We have not observed this window. Option D is now possible, since `etag` is in the file info, but no consumer sends `If-Match` yet.

### Decision 2: Replace the HW1 routes, or keep them

Three HW1 routes changed: `POST /files/upload`, `POST /files/move` and `GET /folders/list`. The plan was to remove them, with no aliases.

**Outcome: changed during implementation** (change 1 above). Upload and move stay as extensions beside the shared routes, and `GET /folders/list` is removed. The plan's objection to aliases was that an alias keeping HW1's `422` would answer the same mistake two ways. That doesn't apply: one app-level handler makes every validation failure `400`, on old and new routes alike. A HW1 caller that checks for `422` on bad input still has to change. Upload and move now behave as Team 3 extensions, not as the shared operations: `POST /files/upload` with `overwrite=true` has no revision check, and `POST /files/move` moves folders.

### Decision 3: Where the shared error body is produced

`SharedErrorRoute` built `{"error": {"code", "message"}}` only for requests that reached a route. FastAPI answered every other request with its own body. On Nov 3, `GET /no-such-route` returned `404 {"detail": "Not Found"}`, which a consumer could read as a missing file.

| Option | What changes | Consequence |
|---|---|---|
| A. Keep `SharedErrorRoute` only | Validation status 422 becomes 400 | Unknown routes and wrong methods keep FastAPI's body |
| B. Replace it with app-level exception handlers | The error mapping and its logging move to new handlers | Every response uses the shared body |
| C. Keep `SharedErrorRoute`, and add app-level handlers for 404, 405 and 500 only (**chosen**) | One status change, plus three handlers | Every response uses the shared body; route errors keep their mapping and logs |

**Outcome: implemented** (`f670154`). Observed on the test service: `GET /folders/list` → `404 route_not_found`, `POST /files/metadata` → `405 method_not_allowed`. `test_unknown_route_returns_404_route_not_found` checks the body. The only responses that still don't use the shared body come from Cloud Run's front end, before the request reaches the service (see the 32 MiB limit below).

### Possible revisit reasons

| Decision | We revisit it if | Owner |
|---|---|---|
| 1 | A consumer needs protection against changes made since *it* read the file; we then accept `If-Match` with the `etag` (option D) | Zesan, with Team 1's create and replace clause owners (Annie, Shini) |
| 2 | The vertical asks services to expose only the shared routes; we then remove the extensions in a release with notes | Shreyas |
| 3 | We adopt the shared Python package's error classes (open question 1); we then map those instead of built-in exceptions | John, Victor |

## Failure behavior

FastAPI validates requests before a route calls the provider-independent `Client`. The client translates Dropbox errors into built-in exceptions; `SharedErrorRoute` maps those to the shared status and code; app-level handlers cover validation, unknown routes, wrong methods and unexpected exceptions.

| Outcome | Exception | HTTP response |
|---|---|---|
| Invalid request or path | `RequestValidationError` | `400 invalid_request` |
| Upload over Dropbox's limit | `FileTooLargeError` | `413 file_too_large` |
| Not allowed (folder moved with `PATCH`, folder downloaded, root deleted, …) | `ValueError`, `IsADirectoryError` | `422 invalid_operation` |
| Nothing at the path | `FileNotFoundError` | `404 not_found` |
| Destination exists, or the file changed during replace | `FileExistsError` | `409 conflict` |
| Permission denied | `PermissionError` | `403 permission_denied` |
| Dropbox unreachable, timed out or rate limiting | `ConnectionError` | `503 provider_unavailable` |
| Other Dropbox failure | `OSError` | `502 provider_error` |
| More than 10 requests in flight | — (middleware) | `503 service_busy` |
| Anything else | any other exception | `500 internal_error` |

The messages for 502, 503 and 500 are fixed, so Dropbox's own error text reaches only the logs. Other messages may contain the caller's paths.

**No automatic retries.** A provider failure can happen after Dropbox has completed a write, so a 502 or 503 doesn't prove that nothing changed. Callers should check metadata before retrying an uncertain write.

**Upload size on Cloud Run.** Cloud Run limits HTTP/1 request bodies. On Nov 11, a 31 MiB upload to the test service returned `201`. A 33 MiB upload returned `413` with an HTML page from Google's front end, so the service never saw it. The deployed service's real limit is therefore about 32 MiB, and above it the caller gets neither the shared body nor `file_too_large`. Locally, the limit is Dropbox's 150 MB. Open question 13 tracks this.

### Configuration, credentials, and data exposure

The service builds its storage client in `build_storage_client()` at startup. Missing Dropbox credentials stop the service from starting, with one sanitized line that names the missing setting but never its value. On Cloud Run, the three Dropbox credentials come from Secret Manager. They are never in the image, the repository, requests or logs. `MAX_IN_FLIGHT_REQUESTS` (10) sets the per-instance limit; excess requests receive `503 service_busy` straight away instead of waiting in a queue.

The service never logs file contents, paths, query strings or headers. Quoted text in failure reasons is replaced with `…`. Cloud Run's own request log records full URLs, query strings included, and so contains paths; it is readable only by project members. Download responses return the file's bytes, and metadata responses include IDs and paths, by design.

The service doesn't authenticate its callers: both Cloud Run services allow unauthenticated requests, and anyone with the URL can use the environment's Dropbox app folder. Each environment has its own Dropbox app, so test can't reach demo's files. This is accepted for coursework (see [Review](#review)).

## Deployment as built

The Nov 4 plan proposed these tools; all were built as planned except telemetry (change 7). Details: [Infrastructure](docs/infrastructure.md), [Release Pipeline](docs/release-pipeline.md), [Operations](docs/operations.md).

| Need | Tool | As built |
|---|---|---|
| Artifact | Docker image from `uv.lock` (`uv sync --locked`) on Python 3.12 | Tagged with the full commit SHA, never `latest`, and deployed by digest. The image sets `RELEASE` to the commit SHA. Images are about 53–70 MB. |
| Registry | Google Artifact Registry, `us-central1` | A cleanup policy keeps the 5 most recent versions. 300 MB stored on Nov 11. |
| Hosting | Cloud Run: `cloud-storage-service-test` and `cloud-storage-service-demo` | 1 vCPU, 512 MiB, scale to zero, at most 2 instances, 20 concurrent requests per instance against our limit of 10. Startup and liveness probes call `GET /health` only. |
| Secrets | Secret Manager: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` per environment | Each runtime service account can read only its own environment's three secrets. |
| Infrastructure as code | OpenTofu with the Google provider; state in a versioned Cloud Storage bucket | Applied Nov 11 by the project owner. CI runs `tofu fmt -check` and `tofu validate` on every push. |
| Pipeline | CircleCI, signing in to Google Cloud through workload identity federation (OIDC) | No Google key is stored anywhere. Only pipelines on `hw2` and `main` can get the deployer identity. Each release is deployed with no traffic, smoke-checked against real Dropbox, then given 100% of traffic. |
| Telemetry | Cloud Logging, Cloud Trace, Cloud Monitoring | One JSON log line per request; traces with Dropbox spans; three log-based metrics; one dashboard; one 5xx alert emailed to the team. |

**Identities.** CircleCI's `storage-deployer` service account can push images and deploy the two services. It can't read secret values, change IAM, or create or delete resources. Each service runs as its own runtime account.

**Capacity.** The overload check ([Overload Protection](docs/overload-protection.md)) confirmed that requests beyond 10 per instance get `503 service_busy` in the shared body, not a Cloud Run error. At most 2 instances bound both cost and the rate of calls to Dropbox.

### Cost, ownership and teardown

Measured from the project's own usage, Nov 5–11, 2026 (test was deployed from Nov 11; demo serves only the placeholder). Free tiers are from Google Cloud's pricing pages.

| Item | Measured use | Free tier | Cost |
|---|---|---|---|
| Cloud Logging ingestion (request logs, our JSON lines, Cloud Run system logs) | 2.9 MB in 7 days | 50 GiB per project per month; then $0.50/GiB | $0 |
| Cloud Monitoring: our 3 log-based metrics (charged by bytes ingested) | ≈ 0.015 MiB in 6 days (487 points) | 150 MiB per billing account per month; then $0.258/MiB | $0 |
| Cloud Monitoring: Cloud Run's own metrics | — | Google Cloud metrics aren't charged | $0 |
| Cloud Trace | 297 spans in 7 days | 2.5 million spans per month; then $0.20/million | $0 |
| Alerting (1 policy) | — | Charges for alerting policies start Sept 1, 2027, after teardown | $0 |
| Cloud Run | 492 s of billable instance time on test, 24 s on demo | 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests per month | $0 |
| Artifact Registry | 300 MB stored | 0.5 GB per month | $0 |
| Secret Manager | 6 secret versions | 6 active versions per month | $0 unless a rotation leaves old versions enabled |
| OpenTofu state bucket | a few KB | 5 GB-months in `us-central1` | $0 |

**Total so far: $0.** Telemetry ingestion is the item that grows with traffic: at about 10 times the measured rate, a full month is still under 0.5% of the logging free tier. The main risks are a load test that runs for hours, or a rotation that leaves old secret versions enabled. A **$5 budget alert** emails the owner either way.

| Owner | Responsibility |
|---|---|
| Shreyas Kaldate | Google Cloud project `ospsd-team3` and its billing account (his own card); the budget alert; `tofu apply` and `tofu destroy` |
| Team 3 | The Dropbox apps for test and demo, which are outside OpenTofu |

**Teardown: Dec 18, 2026**, after HW2 grades are returned, following [Teardown](docs/teardown.md): `tofu destroy`, delete the state bucket, delete the Dropbox apps by hand, then delete the project. The CircleCI project stays, because HW1 uses it.

**Alternatives considered** (unchanged from Nov 4): Render's free plan stops after 15 minutes idle and takes about a minute to wake, which would fail smoke checks and distort the latency SLI. GitHub Actions could also sign in without a key, but moving our CircleCI jobs adds work without adding a capability.

## Migration

The Nov 4 work breakdown, with its outcomes.

| Task | Owner(s) | Outcome on Nov 11 |
|---|---|---|
| Workspace and Python 3.12 | Shreyas | Python 3.12 done. The shared Python package is not pinned yet (change 5, open question 1). |
| Dropbox adapter: replace with `update(rev)`, path rule, `check_health()`, new metadata model, upload limit | Zesan, Jing | Done. The stale-rev behaviour is verified live, and the upload limit is `413 file_too_large`. |
| Shared HTTP routes and error handlers (Decision 3) | John (routes), Victor (error handlers) | Done: all shared routes, the 400/404/405/500 handlers, and root delete `422`. 293 service unit tests. |
| Conformance suite and docs | Shreyas | The suite runs in CI (55 passed). `README.md` and `TUTORIAL.md` were updated for the shared routes on Nov 11, with the tutorial run against the deployed test service. |

**Early end-to-end testing**, as planned: `scripts/consumer_workflow.py` runs the vertical's shared workflow against every new revision before it gets traffic. The steps are upload, metadata, download and compare bytes, list, move, metadata at the new path, delete, then a `404`. Two faulty releases were stopped this way ([Rollback Drill](docs/rollback-drill.md)).

**Incompatible contract changes.** If a defect in the shared contract is found:

1. Affected consumers are Team 1 (Dropbox) and Team 6 (S3/OCI).
2. Whoever finds it doesn't patch the contract alone. They open an issue on `Dayana20/ospsd26-cloud-storage` and start an async review in `#document-storage`. Approval comes from the coordinator (Dayana, Team 1) and the implementation leads (Yashwanth, Team 1; Jing Qian, Team 3; Debasmith Mandal, Team 6).
3. The coordinator tags a new version and posts the migration diff. Each team updates its pin and adapts its service.

## Testing

| Layer | What it checks | Where | Count (Nov 11) |
|---|---|---|---|
| Fast unit tests | Routes, error mapping, telemetry, overload and faults, with `FakeStorageClient`; Dropbox error translation, with a mocked SDK | `src/*/tests/` | 586 (293 in the service); coverage at least 85% |
| Live (real Dropbox) | Provider behaviour we depend on, in uniquely named scratch folders deleted afterwards. Skipped without credentials. | `tests/integration/` | 23 |
| Conformance | Our HTTP behaviour matches the vertical's | CI job `conformance` (in-memory client), and by hand against the deployed test service | 55 passed in both |
| Release smoke check | The shared workflow against each new revision, with real Dropbox, before it gets traffic | `scripts/deploy.sh` | Every deploy |

### Guarantees and how we check them

| Guarantee | Check |
|---|---|
| Create never overwrites, even identical bytes | Live: `test_upload_onto_existing_file_without_overwrite_is_409`, `test_repeating_an_identical_upload_is_409` |
| Replace returns `404` for a missing file and refuses a concurrent change | Live: `test_replace_missing_file_raises_and_creates_nothing`, `test_replace_against_stale_revision_is_a_conflict` |
| `id` stays the same across move and replace | Live: `test_rename_then_move_round_trip`, `test_replace_existing_file_changes_content_and_keeps_id` |
| Move never overwrites; a folder can't be moved into itself | Live: `test_missing_source_and_conflict`, `test_folder_moved_into_itself_is_invalid_operation` |
| List is sorted by path | Fast: `test_list_folder_is_sorted_by_path`; live: `test_list_folder_contents_and_errors`; conformance |
| `modified_at` is UTC with `Z` | Fast: `test_from_file_converts_modified_time_to_utc`, `test_modified_is_timezone_aware_utc` |
| Delete removes folder contents; deleting again is `404`; the root is never deleted | Live: `test_delete_folder_removes_contents`, `test_delete_file_then_repeat_returns_404`; fast: `test_deleting_the_root_returns_422_and_changes_nothing`, `test_delete_root_is_rejected_without_calling_dropbox` |
| Every failure uses the shared body | Fast: failure tests in each route test file, `test_unknown_route_returns_404_route_not_found` |
| Provider health reports Dropbox being down, and `/health` never calls it | Fast: `test_provider_health_is_503_when_dropbox_fails`, `test_health_returns_200_even_when_provider_is_down`; live: `test_check_health_succeeds_against_real_dropbox` |

### Concerns and gaps

- **Accepted: the 150 MB Dropbox limit isn't tested against real Dropbox**, because uploading such a file in every run is too slow. Our size mapping is tested with a mock. On Cloud Run the effective limit is about 32 MiB anyway (open question 13).
- **Accepted: a folder delete that fails partway** can only be simulated with a mock.
- **Accepted: the replace window** (Decision 1) isn't tested, because it needs another client to delete the file between our two calls.
- **Gap: the conformance suite runs against the deployed service only by hand**, not in the pipeline. The pipeline runs the shared workflow instead.

## Open questions and review

"Memo Q" numbers refer to the open questions in the Oct 28 memo.

### Open questions

| # | Question | Status on Nov 11 | Owner |
|---|---|---|---|
| 1 | Depend on the shared contract package (memo Q11) | **Open.** Tag `0.3.1` has the storage contract (`cloud_storage_api`), but its `DocumentStorage` interface differs from our `Client`, and we haven't pinned it. Our HTTP behaviour matches it. | Shreyas |
| 2 | 409 or 422 for an occupied destination or a stale replace (memo Q3) | **Settled: `409 conflict`**, as in contract `0.3.1` | Darsh (Team 6) |
| 3 | One code name per status | **Settled:** contract `0.3.1`'s error table, which `errors.py` follows | Darsh; John and Victor implemented |
| 4 | 404 means only a missing file; a 502/503 after a write means the outcome is unknown (memo Q8) | **Implemented** (`route_not_found`; no retries) | Darsh |
| 5 | Status for a folder delete that fails partway (memo Q6) | **Settled: `502 provider_error`**, as in contract `0.3.1` | John |
| 6 | What deleting the root returns | **Implemented: `422 invalid_operation`**, observed on the test service. The shared contract doesn't state it. | John, with Laya (Team 1) |
| 7 | Replace can recreate a file deleted between its two calls | **Accepted risk**, still unobserved (Decision 1) | Zesan |
| 8 | Dropbox's maximum path length | **Open.** Not measured. | Victor |
| 9 | Upload limit unit and status (memo Q9) | **Settled: `413 file_too_large`**, at Dropbox's 150 MB. See 13 for Cloud Run. | Zesan |
| 10 | `modified_at` in UTC with `Z` | **Settled**, in contract `0.3.1` and implemented | Jing |
| 11 | What Dropbox returns for a stale rev with `autorename=False` | **Settled:** `path/conflict/file`, observed live on Nov 8 | Zesan |
| 12 | Keep `POST /files/rename` as an extension | **Settled: kept**, with the other extensions (change 1) | Team 3 |
| 13 | Cloud Run refuses uploads above about 32 MiB with an HTML `413`, before the service sees them | **Open, new.** Options: document 32 MiB as our deployed limit and tell Teams 1 and 6; or check size in the service below 32 MiB so callers get `413 file_too_large` in the shared body, for requests that declare their length; or enable HTTP/2 end to end, which Cloud Run doesn't limit the same way (unverified). | Zesan |

### Review

| Review | Reviewer | Scope | Status on Nov 11 |
|---|---|---|---|
| Whole design | Victor Wang (Team 3) | Every section | Not recorded yet |
| Contract-facing parts | Darsh (Team 6) | API and data model, failure behavior and open questions | Not recorded yet |

Each review states what the reviewer checked, for example "compared every route and status in this document with contract `0.3.1` and the observed responses". Reviewers comment on the pull request that updates `DESIGN.md`. Each substantive comment gets a disposition below: a change with a link, an answer, or a reasoned decision to keep the approach. Resolving a thread alone is not a disposition.

| # | Comment and reviewer | Disposition | Link |
|---|---|---|---|
| — | No comments recorded yet | — | — |

**Risks we propose to accept** (reviewers confirm or reject each one):

- Replace can recreate a file deleted between its two Dropbox calls (Decision 1, question 7).
- The 150 MB limit isn't tested against real Dropbox, and a partial folder delete is simulated only with a mock.
- Dropbox treats paths that differ only in letter case as one file. The contract documents this, and we don't work around it.
- The deployed services don't authenticate callers. Anyone with a URL can read and write that environment's Dropbox app folder until teardown on Dec 18.

**Scope of approval.** Approval accepts this design at the reviewed revision. It doesn't show that the implementation is correct; the conformance suite, the live tests and the release smoke check do that.

## Conclusion

Team 3's service now implements the vertical's shared HTTP contract: one route per shared operation, one file-info object, and one error body with the contract's status codes. It passes the shared conformance suite, and it is deployed to the test environment through a pipeline that checks each release against real Dropbox before it serves traffic. During implementation we kept the HW1 upload and move routes as extensions, restricted `PATCH` to files to match the suite, added `etag` and `413`, and moved telemetry to Google Cloud. Three things remain: pinning the shared Python package, deciding how to handle Cloud Run's 32 MiB upload limit, and the first deployment of demo from `main`.
