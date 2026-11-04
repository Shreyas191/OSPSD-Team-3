# Team 3 Design Document

**By:** John Ashik, Shreyas Kaldate, Zesan Rahman, Jing Qian, Victor Wang
**Vertical:** Document Storage (provider: Dropbox)

**Last updated:** 11/4/2026

**Links:**
- Shared contract: tag [`0.1.0`](https://github.com/Dayana20/ospsd26-cloud-storage/tree/0.1.0)
- Decision memo: <https://docs.google.com/document/d/1K9U8uO-FVbeu0PGlWpr_frJ6YQmoHXdz/edit?usp=sharing&ouid=104071874439967208816&rtpof=true&sd=true>
- Implementation work:
  - Team 3 GitHub: <https://github.com/Shreyas191/OSPSD-Team-3>
  - Unified API GitHub: <https://github.com/Dayana20/ospsd26-cloud-storage>

## Introduction

The Document Storage vertical provides one HTTP contract for consumers that need to store and manage files across Team 1, Team 3, and Team 6 services. Consumers should be able to use the same request formats and interpret responses consistently, even when the services use different storage providers behind the API.

### Context

The three services currently support seven overlapping operations: upload, replace, move, list, download, metadata, and delete. However, their current APIs differ in ways that force consumers to write provider-specific code. For example, uploading a file uses `POST /files` with `file` and `path` on Team 1, `POST /files/upload` with `file` and `remote_path` on Team 3, and `POST /files/{key}` on Team 6. The error bodies and metadata fields also differ. Some behavioral differences could cause unexpected results: Team 3 deletes a folder and its contents, while Team 1 rejects folder deletion. A consumer that switches services may need to change its requests and could get different behavior for the same operation. This becomes especially problematic for the consumer if they are working with multiple services and need to unnecessarily keep track of how each service works.

### Goals and non-goals

Our goals are the following, in no specific order:

- Give each common operation one agreed route, HTTP method, and request shape across all participating services.
- Return a consistent file information object for operations such as upload, download, metadata lookup, and move.
- Use a shared error response format with consistent meanings for HTTP statuses and error codes, so consumers can handle errors without relying on provider-specific messages.
- Make operations with potentially different effects, such as creating versus replacing a file or deleting a folder, behave according to the same documented contract.
- Allow a consumer's common workflow to run against each participating service with the same requests and outcome handling, changing only the base URL, credentials, and provider-specific identifiers.
- Ensure that all operations and their routes follow RESTful practices for consumer clarity.

Non-goals, or goals that you the reader may expect us to reach but we are not proactively chasing, are the following (they may be changed or expanded):

- Migrating existing files, folders, or identifiers between providers.
- Exposing every capability offered by each storage provider through the shared contract.
- Switching storage providers during an in-progress request.
- Standardizing authentication and credential provisioning. Each service may continue to use its own credentials and setup process; consumers provide the credentials required by the service they call.

## Proposed architecture

**Figure 1. How a consumer's request reaches Dropbox, and which package owns each step.** Solid arrows are calls at request time. The `◄──` arrow means "implements": `dropbox_client_impl` implements the shared `Client` contract. The service never imports `dropbox`.

```text
                    ┌──────────────────────────┐
                    │   HTTP Client / Caller   │
                    └────────────┬─────────────┘
                                 │ HTTP (REST + ?path=)
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ cloud_storage_service                                              │
│   ├── ConcurrencyLimitMiddleware   (In-flight overload protection) │
│   ├── App-Level Exception Handlers (404, 405, 500 error envelope)  │
│   ├── FastAPI Unified Routes       (HTTP -> Client contract calls) │
│   └── SharedErrorRoute             (Domain exception -> HTTP error)│
└────────────┬──────────────────────────────────┬────────────────────┘
             │ calls abstract contract          │ instantiates at startup
             ▼                                  ▼
┌──────────────────────────────────┐   ┌──────────────────────┐
│ ospsd26-cloud-storage            │◄──│ dropbox_client_impl  │
│   (Shared Client & File contracts)│   │   (DropboxClient)    │
└──────────────────────────────────┘   └──────────┬───────────┘
                                                  │ SDK calls
                                                  ▼
                                       ┌──────────────────────┐
                                       │     Dropbox API      │
                                       └──────────────────────┘
```

**Responsibilities and dependency direction.**

| Component | Owns | Depends on |
|---|---|---|
| `cloud_storage_service` | HTTP routes, request validation, overload protection, the shared error body and failure logs | The shared `Client` contract only |
| Shared contract (`ospsd26-cloud-storage`; today our local `cloud_storage_client_api`) | The abstract `Client` and `File` classes and `get_client()` | Nothing |
| `dropbox_client_impl` | `DropboxClient`, `DropboxFile`, OAuth in `auth.py`, and the translation of Dropbox errors to built-in exceptions | The contract and the Dropbox SDK |

**Who creates and closes the provider client.** The app's `lifespan` calls `build_storage_client()` once at startup and stores one `DropboxClient` in `app.state.storage_client`. Routes get it through the `get_storage_client` dependency, so tests can swap in `FakeStorageClient`. The client lives as long as the process. We do not close it explicitly; the Dropbox SDK's HTTP session ends when the process exits.

### Workflow guarantees and processing layers

Items marked **(planned)** are part of the migration and are not in the code at `f2127d7`.

- **Path normalization (`clean_up_path`, planned):** the service accepts paths with or without a leading `/`. It turns each path into the form `/path/to/resource` and removes trailing slashes and empty segments. It rejects `.` and `..` segments with 400 `invalid_request`. A delete of the root (`?path=/`) returns 422 `invalid_operation`.
- **Syntactic vs. semantic error boundary:**
  - **HTTP 400 (`invalid_request`):** malformed paths, missing required arguments, or uploads over 150 MiB. A handler turns FastAPI's `RequestValidationError` into 400 (planned; today it is 422).
  - **HTTP 422 (`invalid_operation`):** semantically invalid domain operations (e.g. moving a folder into itself, or deleting root).
  - **HTTP 404 (`not_found`):** the target file or directory does not exist.
  - **HTTP 409 (`conflict`):** the target destination already exists on a strict create (`POST /files`), move, or rename conflict.
  - **HTTP 502 (`provider_error`):** upstream provider error or partial folder deletion.
  - **HTTP 503 (`provider_unavailable` / `service_busy`):** service concurrency shed, or the remote provider is unreachable.
- **Domain data model (`File`):** standardized across all operations. Returns `id` (opaque string), `name`, `path` (canonical absolute path), `size` (bytes; null for folders), `modified_at` (ISO 8601 UTC ending in `Z`; null for folders), and an optional `etag` (planned). The HW1 `is_folder` boolean is dropped from the metadata model in favor of a context-specific `type` field (`"file"` or `"folder"`) in folder listing responses.
- **Split health probes (planned):** `GET /health` answers 200 `{"status": "ok"}` without calling Dropbox, so a Dropbox outage never restarts the container. `GET /health/provider` makes one `users_get_current_account()` call. It returns 200 when the credentials work and 503 when Dropbox is unreachable.
- **Structured logging:** application logs record failures with structured key-value pairs (`operation`, `status`, `code`, `provider_failure`, `reason`). 502 and 503 responses carry a fixed message; the provider's own error text goes only to the logs.

### Request flow

**Trace: successful request (`GET /files/metadata?path=/Docs/spec.pdf`)**

1. **Caller route:** the caller sends `GET /files/metadata?path=/Docs/spec.pdf`.
2. **Validation:** FastAPI matches the route and validates that `path` conforms to `_ABSOLUTE_PATH_PATTERN`.
3. **Dependency resolution:** FastAPI resolves the `StorageClient` dependency by retrieving `request.app.state.storage_client`.
4. **Contract invocation:** the route calls `client.get_metadata("/Docs/spec.pdf")`.
5. **Adapter dispatch:** `DropboxClient.get_metadata()` calls `self.dbx.files_get_metadata("/Docs/spec.pdf")`.
6. **Provider call:** the Dropbox API returns a `FileMetadata` object.
7. **Domain conversion:** `DropboxClient` wraps `FileMetadata` in a `DropboxFile` instance and returns it to the route.
8. **HTTP response:** the route invokes `FileResponse.from_file(file)`. FastAPI serializes it to JSON and returns `200 OK`:

   ```json
   {
     "id": "id:3jh89d...",
     "name": "spec.pdf",
     "path": "/Docs/spec.pdf",
     "size": 1048576,
     "modified_at": "2026-10-24T12:00:00Z"
   }
   ```

**Trace: expected failure (`GET /files/metadata?path=/Docs/missing.pdf`, 404 Not Found)**

1. **Caller route:** the caller sends `GET /files/metadata?path=/Docs/missing.pdf`.
2. **Validation:** the path format is validated successfully.
3. **Contract invocation:** the route calls `client.get_metadata("/Docs/missing.pdf")`.
4. **Provider error:** `self.dbx.files_get_metadata()` raises `dropbox.exceptions.ApiError` with tag `GetMetadataError.path(LookupError.not_found)`.
5. **Error translation (adapter):** caught by `_translate_get_metadata_error()`, which raises Python's built-in `FileNotFoundError("No file or folder at '/Docs/missing.pdf'")`. No Dropbox types escape the adapter.
6. **Error handling (route class):** `SharedErrorRoute` catches `FileNotFoundError`.
7. **Classification and logging:** `_classify()` maps `FileNotFoundError` to HTTP 404 and code `"not_found"`. `log_failure()` writes structured key-value failure logs.
8. **HTTP response:** returns `404 Not Found`:

   ```json
   {
     "error": {
       "code": "not_found",
       "message": "No file or folder at '/Docs/missing.pdf'"
     }
   }
   ```

## API and data model

**Contract source.** The shared HTTP contract and Python package (Python 3.12) will live in [Dayana20/ospsd26-cloud-storage](https://github.com/Dayana20/ospsd26-cloud-storage). Its first tag, `0.1.0` (Nov 4), contains the course's mail-client template, not the storage models, errors or routes, so this section follows the Oct 28 decision memo. Unresolved points are marked **(open)**.

### Operations

| Operation | Route | Request | Success |
|---|---|---|---|
| Health | `GET /health`, `GET /health/provider` | none | `200 {"status":"ok"}` |
| Create | `POST /files` | multipart: `file`, `path` | `201` file info |
| Replace | `PUT /files` | multipart: `file`, `path` | `200` file info |
| Metadata | `GET /files/metadata?path=` | `path` | `200` file info |
| Download | `GET /files/download?path=` (open) | `path` | `200` raw bytes |
| List | `GET /files?path=` | folder `path` | `200` array, sorted by path |
| Move or rename | `PATCH /files?path=<from>` | `{"new_path": "<to>"}` | `200` file info |
| Delete | `DELETE /files?path=` | `path` | `204` |

### File-info object

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Opaque Dropbox ID. It stays the same across move, rename and replace; a copy gets a new one. |
| `name` | string | The last path segment |
| `path` | string | Canonical path starting with `/` |
| `size` | integer or null | Bytes; `null` for folders |
| `modified_at` | string or null | ISO 8601 UTC with `Z` (open); `null` for folders |
| `type` | `file` or `folder` | List entries only |

### Key choices

- **IDs** are unique only within our Dropbox app folder, and don't carry over to other teams' services.
- **Paths** are sent in `?path=` and normalized to start with `/`. Empty paths and `.`/`..` segments are rejected. Dropbox paths are case-insensitive.
- **Create** never overwrites, not even identical bytes (we upload with `strict_conflict=true`).
- **Replace** returns `404` if the file is missing, and refuses the write if someone changed the file since we read it (`WriteMode.update(rev)`; 2 Dropbox calls).
- **Move** never overwrites an existing destination.
- **Delete** also removes a folder's contents.
- **List** is one level deep; we fetch every Dropbox page, so the consumer never handles cursors.

### Errors

All errors use `{"error": {"code", "message"}}`. Consumers should branch on the status and code, not the message.

| Status | When |
|---|---|
| `400` | Malformed request |
| `404` | Path doesn't exist |
| `409` (or `422`, open) | Destination already exists |
| `422` | Invalid operation, e.g. a folder moved into itself |
| `403` | Dropbox denied access |
| `502` | Dropbox failed, including a partial folder delete |
| `503` | Dropbox unreachable or not configured |

After a `502` or `503` on a write, the outcome is uncertain: check metadata before retrying.

### Example (expected, from the memo; not yet observed, because `PATCH /files` is not built)

```http
PATCH /files?path=/Docs/report.pdf
{"new_path": "/Archive/report.pdf"}

200 OK
{"id": "id:AbC123", "name": "report.pdf", "path": "/Archive/report.pdf", "size": 1234, "modified_at": "2026-10-23T12:00:00Z"}
```

### What changes from HW1

- **Upload:** `POST /files/upload` with `overwrite` becomes `POST /files` and `PUT /files`.
- **Move:** `POST /files/move` with `from_path`/`to_path` becomes `PATCH /files` with `new_path`.
- **List:** `/folders/list` becomes `GET /files`, adding `type` and sorting.
- **Metadata:** drop `is_folder`, and rename `modified` to `modified_at`.
- **New:** the health endpoints.
- **Kept as Team 3 extensions:** rename, copy, create folder and search.

**Information lost:**

- metadata no longer says whether a path is a folder, though list's `type` does;
- replace takes 2 Dropbox calls instead of 1;
- case-only renames are refused.

## Alternatives

This section compares alternatives for three decisions. Decision 1 matters most to consumers, because the wrong choice loses data without an error. Labels: **Decided** means agreed in the Oct 28 memo, **Proposed** means Team 3's plan for review, and **Assumed** means not yet verified.

### Decision 1: How `PUT /files` replaces a file (Proposed)

The memo says replace returns `404 not_found` for a missing file and never creates one. Its replace clause also plans a revision check, so Dropbox refuses a concurrent change instead of overwriting it. The memo leaves open how Team 3 makes that check. The open PR #37 adds `PUT /files`, but it meets neither rule. It calls `files_upload` with `WriteMode.overwrite`, which creates a missing file and overwrites any change.

| Option | Dropbox calls | Missing file | File changed by someone else |
|---|---|---|---|
| A. `files_upload` with overwrite (PR #37 today) | 1 | Created, which breaks the memo's 404 rule | Overwritten without an error |
| B. `files_get_metadata`, then `files_upload` with overwrite | 2 | `404 not_found` | Overwritten if it changes between our two calls |
| C. `files_get_metadata`, then `files_upload` with `update(rev)` (**chosen**) | 2 | `404 not_found` | Refused if it changes between our two calls (status: open question 2) |
| D. The consumer sends the revision it last read, in an `If-Match` header | 1 | `404 not_found` | Refused if it changed since the consumer read it |

**Evidence.** Dropbox's [upload documentation](https://www.dropbox.com/developers/documentation/http/documentation#files-upload) says update mode overwrites only "if the given rev matches the existing file's rev". The 404 rule already needs the metadata call, so passing its rev to the upload adds no call. Option D needs a version field in the shared file-info object, and the memo has none: Team 6 keeps `etag` only as an optional field.

**Cost.** Each replace makes 2 Dropbox calls instead of 1. The check covers only the interval between our two calls, not a consumer that read the file minutes earlier; only option D covers that. The same documentation says a write to a path with no file "is always written". If another client deletes the file between our two calls, Dropbox therefore recreates it (**Assumed** from the documentation, not yet observed). What Dropbox returns for a stale rev with `autorename=False` is also unverified. A new live test checks both before PR #37 merges (see [Testing](#testing)).

### Decision 2: Replace the HW1 routes, or keep them as aliases (Proposed)

Three HW1 routes change: `POST /files/upload`, `POST /files/move` and `GET /folders/list`. The alternative keeps each old route for one release beside its shared route. Team 6 considered the same option for its own routes in the memo.

Aliases would preserve little. The memo moves bad input from 422 to 400 (**Decided**). An alias that adopts 400 still breaks a HW1 caller that checks for 422. An alias that keeps 422 makes our service answer the same mistake two ways.

**Evidence.** Our tests and `TUTORIAL.md` are the only known callers: these routes appear 33 times in our test files and on 5 lines of `TUTORIAL.md`. The memo tells other verticals not to build against a storage service until the contract is tagged (**Assumed:** no team outside Team 3 calls our HW1 routes).

**Cost.** Every caller of a HW1 route must switch to the shared route. We release the change as `v0.2.0` and list each breaking change in the release notes. We update `TUTORIAL.md`, its 22 screenshots and the README in the same release.

### Decision 3: Where the shared error body is produced (Proposed)

`SharedErrorRoute` in `errors.py` builds `{"error": {"code", "message"}}`, but only for requests that reach one of our routes. FastAPI answers every other request with its own body. We observed this on Nov 3 with FastAPI's test client at commit `f265246`:

- `GET /no-such-route` returns 404 `{"detail": "Not Found"}`.
- `PUT /files` returns 405 `{"detail": "Method Not Allowed"}`.

A consumer that branches on 404 would read a mistyped route as a missing file. Team 6 asks the vertical to make 404 mean only that the file is missing (memo, Errors; not yet agreed).

| Option | What changes | Consequence |
|---|---|---|
| A. Keep `SharedErrorRoute` only | Validation status 422 becomes 400 | Unknown routes and wrong methods keep FastAPI's body |
| B. Replace it with app-level exception handlers, as Team 6 did in HW1 | The error mapping and its logging move to new handlers | Every response uses the shared body |
| C. Keep `SharedErrorRoute`, and add app-level handlers for 404, 405 and 500 only (**chosen**) | One status change in `errors.py`, plus three handlers | Every response uses the shared body; route errors keep their mapping and logs |

**Evidence.** Every route already goes through `SharedErrorRoute`, and our 82 route tests run against it. Shared-repo issue #4 proposes the codes `route_not_found`, `method_not_allowed` and `internal_error` for the cases no route handles.

**Cost.** Error bodies come from two places, so both must follow one code table. A test in each place checks the body shape.

### Why we are sticking with our choice

- **Decision 1, option C:** it is the only option that meets both memo rules (404 for a missing file, no silent overwrite) without a new field. Its extra Dropbox call is the one the 404 rule needs anyway.
- **Decision 2, replace:** no known consumer outside Team 3 calls the HW1 routes, and aliases cannot keep HW1's statuses without contradicting the memo. Each operation keeps one route, one handler and one set of tests.
- **Decision 3, option C:** it makes 404 unambiguous and leaves the mapping our 82 route tests check unchanged. Option B would rewrite tested code without changing what consumers see.

### Possible revisit reasons

| Decision | We revisit it if | Owner |
|---|---|---|
| 1 | The live test shows that `update(rev)` accepts a stale revision, or fails with an error we cannot map | Zesan |
| 1 | The vertical adds a version field such as `etag` to the shared file-info object; we then adopt option D | Zesan, with Team 1's create and replace clause owners (Annie, Shini) |
| 2 | A team outside Team 3 reports calling a HW1 route before `v0.2.0` ships; we then keep aliases for one release | Shreyas |
| 3 | The shared Python package ships its own error handlers; we then use those instead | John, Victor |

## Failure behavior

Requests are validated by FastAPI before the route calls the provider-independent `Client`. The client translates provider errors into built-in exceptions; `SharedErrorRoute` maps those exceptions to the shared response shape, `{"error": {"code": "...", "message": "..."}}`. Status codes and code names follow the table proposed in shared-repo issue #4 (open questions 2 and 3); today's service still returns `422 invalid_input` for validation errors and `502 provider_failure`, and changes to these as part of the migration.

| Outcome | HTTP error response |
|---|---|
| Invalid request or invalid path (request validation, `RequestValidationError`) | 400 `invalid_request` |
| Invalid operation, e.g. a folder moved into itself or a folder given to download (`ValueError`, `IsADirectoryError`) | 422 `invalid_operation` |
| Source path not found (`FileNotFoundError`) | 404 `not_found` |
| Destination already exists (`FileExistsError`) | 409 `conflict` |
| Permission denied (`PermissionError`) | 403 `permission_denied` |
| Provider connection failure (`ConnectionError`) | 503 `provider_unavailable` |
| Internal error (`OSError`) | 502 `provider_error` |
| Too many requests in flight | 503 `service_busy` |
| Unexpected exception | 500 |

For provider failures, callers receive a fixed message rather than provider-specific details. Failures are logged as structured warnings with the operation, status, error code, provider-failure flag, and reason. Request-validation logging summarizes the invalid fields without recording the rejected values. The public messages for 502 and 503 are fixed, so provider-specific details are not returned to callers. Other domain error messages may include storage paths.

The service does not implement application-level automatic retries. A provider failure can occur after the provider has completed a write, so a 502 or 503 does not prove that no change was made. Callers should check metadata before retrying an uncertain write.

### Configuration, credentials, and data exposure

The service selects and builds its storage client in `build_storage_client()` at startup. The current implementation selects Dropbox, and missing Dropbox credentials prevent the service from starting. Supply provider credentials through protected deployment configuration or a secrets store; do not commit them, bake them into the service artifact, or include them in requests or logs. `MAX_IN_FLIGHT_REQUESTS` configures the concurrency limit; excess requests receive `503 service_busy` rather than waiting in a queue.

The app does not log file contents. Structured warning logs include the operation, status, error code, provider-failure flag, and failure reason. Request validation errors are summarized by field and problem without logging rejected values. Failure reasons and domain error messages can contain paths or provider details, so logs should be access-controlled and checked to ensure they contain no secrets or sensitive provider data. Download responses intentionally return the requested file's bytes, and metadata responses include file identifiers and paths.

The app does not implement authentication for API callers. Provider credentials authorize the service to access the provider; they do not restrict who can call the service's HTTP routes. Deployment must restrict access as appropriate. The app also does not set a request-body size limit.

### Redesign effects

On `dev` (release `v0.1.0`), `POST /files/upload` takes a file, `remote_path` and an `overwrite` flag, and makes one `files_upload` call. Open PR #37 splits it: `POST /files` uploads in add mode and `PUT /files` in overwrite mode, each with one Dropbox call. Both routes copy the upload to a temporary file and delete it when the request ends.

PR #37's `PUT` still creates a missing file and overwrites concurrent changes. Before it merges, Zesan changes it to Decision 1, option C: 2 Dropbox calls, 404 for a missing file and a revision check. Until then, the 2-call cost in this document describes the plan, not the code.

### Deployment tools

**Status: Proposed.** Team 3 has not yet agreed on these tools. We confirm them, name the account owners and fix the teardown date at our next team meeting, before Level 1 work starts. Free-tier figures come from each provider's pricing page, checked on Nov 3, 2026.

Our service keeps no state of its own; every file lives in Dropbox. A deployment therefore needs five things: a container, a place to run it, three Dropbox secrets, a pipeline and telemetry. Each environment uses its own Dropbox app, so each deployment serves one app folder, as the memo assumes.

| Need | Proposed tool | Why | Assumption to check |
|---|---|---|---|
| Artifact | Docker image built from `uv.lock` on Python 3.12, tagged with the commit SHA and deployed by image digest | One immutable artifact per commit. The memo puts the shared package on Python 3.12 | We move from 3.11 to 3.12 once the contract is tagged (open question 1) |
| Registry | Google Artifact Registry | It sits in the same project as the service, so one access policy covers pushing and pulling images | The free tier is 0.5 GB of storage per month. Our image size is unmeasured because no Dockerfile exists yet; a cleanup policy keeps only recent images |
| Hosting | Google Cloud Run, with two services: test (disposable) and demo (the one we demonstrate) | Each deploy creates a fixed revision, and rollback moves traffic back to the previous revision. Startup and liveness probes call only `GET /health` (from the memo's health clause, not built yet), so a Dropbox outage does not restart the container | The free tier is 2 million requests, 180,000 vCPU-seconds and 360,000 GiB-seconds per month. **Estimate:** conformance runs, smoke checks and the demo stay far below that |
| Secrets | Google Secret Manager: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET` and `DROPBOX_REFRESH_TOKEN` for each environment | Cloud Run reads them at startup, so they never enter the image or the repository | 3 secrets × 2 environments = 6 versions, exactly the free tier of 6 active versions per month. A rotation adds a version, so we destroy the old one |
| Infrastructure as Code | OpenTofu with the Google provider. State lives in a versioned Cloud Storage bucket in us-central1 | OpenTofu was covered in lecture. Its gcs backend locks state, so two applies cannot run at once | The free tier is 5 GB-months, and only in us-east1, us-west1 or us-central1 |
| Pipeline | CircleCI, which already runs our checks and holds the `dropbox-client` context | It signs in to Google Cloud with CircleCI's OIDC token through workload identity federation, so no Google key is stored in CircleCI | Only the deploy job on `main` uses the deploy context. Pull requests from forks get no secrets. **Not yet tried** |
| Telemetry | OpenTelemetry SDK in the service, exporting to Grafana Cloud's free plan; alerts go to the team Slack channel | One vendor-neutral SDK, with dashboards, logs, traces and alerts in one place and no credit card | The free plan allows 10,000 active metric series, 50 GB of logs and 50 GB of traces per month, with 14-day retention and 3 users. Our series count is an estimate until Level 3 runs |

**Identities.** We use two kinds of Google service account. CircleCI's deploy account can push images, deploy the two Cloud Run services and write the state bucket; it cannot read secret values. Each service's runtime account can read only its own environment's three secrets. The test service therefore cannot reach the demo service's Dropbox folder.

**Capacity.** We set Cloud Run's per-instance concurrency above `MAX_IN_FLIGHT_REQUESTS`: 20 against our limit of 10. Excess requests then reach our limiter, so a busy caller receives `503 service_busy` in the shared body, not a Cloud Run error. We cap each service at 2 instances, which bounds both cost and the rate of calls to Dropbox. **Assumption:** we check this in Level 1 with `scripts/overload_demo.py`.

**Cost, ownership and teardown**

| Item | Expected cost | Owner | Teardown |
|---|---|---|---|
| Google Cloud project and billing account | $0 within the free tiers above. The free tier still needs a billing account, so a $5 budget alert emails the owner | To be named | `tofu destroy`, then delete the state bucket and the project |
| Grafana Cloud stack | $0 (free plan) | To be named | Delete the stack |
| Dropbox apps for test and demo | $0 | To be named | Delete both apps and their app folders by hand; they are outside OpenTofu |

Proposed teardown date: Dec 18, 2026, after HW2 grades are returned. The CircleCI project stays, because HW1 already uses it.

**Alternatives considered.**

- **Render's free plan instead of Cloud Run.** Render stops a free service after 15 minutes without traffic, and waking it takes about a minute. Its documentation says not to use free services in production, and rollback reaches only the two most recent deploys. A one-minute wake would fail the smoke check and distort the latency SLI.
- **GitHub Actions instead of CircleCI.** It can also sign in to Google Cloud without a stored key. Moving our existing jobs would add work without adding a capability.

**We revisit these choices if:**

- the budget alert fires;
- our metric series exceed Grafana's free limit, in which case we export the same OpenTelemetry data to Google Cloud's own monitoring;
- CircleCI's sign-in to Google Cloud fails in Level 2, in which case we move the deploy job to GitHub Actions.

## Migration

### Scope of changes

- **Runtime environment:** upgrade from Python 3.11 to **Python 3.12** across all `pyproject.toml` workspace files, `.python-version`, and CI Docker base images (memo Q10).
- **Workspace and configuration:**
  - Replace the local workspace source `cloud_storage_client_api` in `pyproject.toml` with the external dependency pointing to the tagged release.
  - Remove the local `src/cloud_storage_client_api/` directory once `uv.lock` pins the external package.
- **Provider adapter (`dropbox_client_impl`):**
  - *Decision 1, option C (PR #37):* replace the single-call overwrite with a two-call, revision-checked replace: call `files_get_metadata()` to read the existing revision, followed by `files_upload(..., mode=WriteMode.update(rev), autorename=False)`. A missing file returns `404 not_found`, and a concurrent change is refused.
  - *Path ingestion:* implement `clean_up_path` to accept paths with or without a leading `/`, stripping trailing slashes, empty segments, and `.` / `..` traversal tokens.
  - *Metadata serialization:* drop `is_folder`; rename `modified` to `modified_at`, serialized in ISO 8601 UTC with a `Z` suffix. Include optional `etag` and `type` (`"file"` or `"folder"` for list entries).
  - *Health probing:* implement `check_health()` with one `dbx.users_get_current_account()` call.
- **HTTP service (`cloud_storage_service`):**
  - *Replace HW1 routes:* retire `POST /files/upload`, `POST /files/move`, and `GET /folders/list`. Introduce the shared REST routes: `POST /files` (create-only, 201), `PUT /files` (replace-only, 200), `PATCH /files?path=<from>` with body `{"new_path": "<to>"}`, `GET /files?path=<folder_path>`, `DELETE /files?path=<path>`, `GET /health`, and `GET /health/provider`.
  - *Error production:* retain `SharedErrorRoute` in `errors.py` for route-level exception translation, and register FastAPI app-level exception handlers for `404` (`route_not_found`), `405` (`method_not_allowed`), and `500` (`internal_error`) so every error response uses `{"error": {"code", "message"}}`.
  - *Validation status code:* map syntactic validation failures (`RequestValidationError`) to `400 invalid_request` instead of FastAPI's default 422. Use `422 invalid_operation` only for semantic domain errors (e.g. moving a folder into itself, or deleting root `?path=/`).
- **Tests, documentation, and examples:**
  - Update all 33 route test occurrences and 5 references in `TUTORIAL.md`.
  - Update all 22 tutorial screenshots and `README.md` to show the unified routes and error bodies.
  - Add live integration tests covering: stale-revision handling on replace, case-insensitive collision behavior, and root deletion rejection (`422 invalid_operation`).
- **Compatibility consequences for existing callers:**
  - Existing HW1 callers cannot rely on aliases. Every caller must update endpoint URLs, move path identifiers to `?path=` query parameters, and switch upload logic between `POST` (create) and `PUT` (replace).
- **E2E testing and merging:** run the end-to-end tests before merging the `dev` branch into `main`.

### Work breakdown

| Task | Owner(s) | Scope of changes | Verification and exit criteria |
|---|---|---|---|
| **Workspace and Python 3.12 setup** | Shreyas | • Bump `.python-version` and `pyproject.toml` to Python 3.12.<br>• Pin the first unified-repo tag that contains the storage contract in `pyproject.toml` and `uv.lock` (not `0.1.0`, which holds the mail-client template).<br>• Remove internal `src/cloud_storage_client_api/`. | `uv sync --all-packages --extra dev`<br>`uv run ruff check .`<br>`uv run mypy src tests` |
| **Dropbox adapter realignment (PR #37)** | Zesan (Replace/Create), Jing (Metadata/List/Health) | • Implement Decision 1, option C (two-call revision-checked replace in `dropbox_impl.py`).<br>• Implement `clean_up_path` and `check_health()`.<br>• Update the metadata model (`modified_at` with `Z`, `type`, drop `is_folder`).<br>• Enforce the strict upload limit (150 MiB) and conflict checks. | Fast unit tests: `uv run pytest src/dropbox_client_impl/tests/`<br>Live integration tests in a uniquely named scratch folder: `test_upload_onto_existing_file_without_overwrite_is_409`, live stale-rev replace test. |
| **Unified HTTP service and error realignment** | John (routes), Victor (error handlers) | • Implement unified routes: `POST /files`, `PUT /files`, `PATCH /files`, `GET /files`, `DELETE /files`, `GET /health`, `GET /health/provider`.<br>• Implement Decision 3, option C: app-level exception handlers for 404, 405, 500.<br>• Re-map `RequestValidationError` to `400 invalid_request` and root deletion to `422 invalid_operation`. | 82 route unit tests passing with in-memory `FakeStorageClient`: `uv run pytest src/cloud_storage_service/tests/` |
| **Conformance suite and doc updates** | Shreyas | • Vendor Team 6's conformance test suite into `tests/conformance/`.<br>• Update `README.md`, `TUTORIAL.md`, and regenerate all 22 tutorial screenshots for `v0.2.0`. | Full test pass against both `FakeStorageClient` and the live Dropbox service in CI. Green CircleCI build. |

### Early end-to-end testing

To catch broken assumptions before the final merge of `dev` into `main`, deploy the service in an isolated staging environment (Google Cloud Run test instance) and run the shared vertical workflow:

1. **Upload file (POST):** upload the file using `POST /files`.
2. **Get metadata:** retrieve file metadata to confirm initialization.
3. **Download and compare bytes:** download the file content and verify byte parity against the original upload.
4. **List folder:** list folder contents using `GET /files?path=<folder_path>` to ensure the item appears.
5. **Move (PATCH):** move the file to a new target location using `PATCH /files`.
6. **Verify metadata at new path:** fetch metadata at the destination path to confirm the move succeeded.
7. **Delete file:** remove the file using `DELETE /files?path=<path>`.
8. **Verify 404:** attempt to retrieve metadata or content at the deleted path to confirm a 404 response is returned.

### Incompatible contract changes

If a defect or missing requirement is uncovered after implementation has begun:

1. **Affected consumers:**
   - Peer storage service implementations: **Team 1 (Dropbox)** and **Team 6 (S3/OCI)**.
2. **Notification and review protocol:**
   - The discovering individual must not patch the contract unilaterally. An issue must be opened on `Dayana20/ospsd26-cloud-storage` outlining the technical defect and impact.
   - The discoverer starts an async review in the `#document-storage` Slack channel.
   - Approval is required from the designated vertical leads before a change is accepted:
     - **Coordinator:** Dayana (Team 1)
     - **Implementation leads:** Yashwanth (Team 1), Jing Qian (Team 3), Debasmith Mandal (Team 6)
3. **Release and migration window:**
   - The coordinator cuts an incremental semantic tag on the unified repository.
   - A release advisory and migration diff are posted to `#document-storage`.
   - Each team then updates its pinned dependency in `pyproject.toml` and adapts its service.

## Testing

We keep HW1's three layers of tests and add the shared conformance suite. Fast tests run on every push without network or credentials; live tests check our assumptions about Dropbox.

### What each layer establishes

| Layer | What it checks | Where |
|---|---|---|
| **Controlled (fast)** | Routes accept the shared request shapes and return the shared file info and error body, using an in-memory fake client. Dropbox errors become the documented exceptions, using a mocked SDK. | `src/*/tests/` |
| **HTTP boundary** | Status codes, response bodies, path normalization and `400` for bad input, sent through FastAPI with the fake client. | `src/cloud_storage_service/tests/` |
| **Live (real Dropbox)** | Provider behaviour we depend on, run in uniquely named scratch folders that are deleted afterwards. Skipped without credentials; runs in CI on `dev`/`main`. | `tests/integration/` |
| **Conformance** | Our service behaves like Team 1's and Team 6's. It's one shared suite, owned by Team 6. | unified repo |

### Guarantees and how we check them

| Guarantee | Check |
|---|---|
| Create never overwrites, even identical bytes | Live: `test_upload_onto_existing_file_without_overwrite_is_409`, `test_repeating_an_identical_upload_is_409` |
| Replace returns `404` for a missing file and refuses a concurrent change | **New** live: `test_replace_missing_file_is_404`, `test_replace_refuses_stale_revision` (changes the file between the two Dropbox calls) |
| `id` stays the same across move and replace | Live: `test_rename_then_move_round_trip`, `test_upload_then_overwrite_round_trip` |
| Move never overwrites; a folder can't be moved into itself | Live: `test_missing_source_and_conflict`, `test_folder_moved_into_itself_is_invalid_input` |
| List returns every entry, sorted, with `type` | Fast: pagination tests in `src/dropbox_client_impl/tests/` (0, fewer than, exactly and more than one page); **new** live: `test_list_is_sorted_and_typed` |
| `modified_at` is UTC with `Z` | **New** fast: `test_modified_at_is_utc_with_z`; **new** live assertion in `test_upload_then_overwrite_round_trip` |
| Delete removes folder contents; deleting again is `404` | Live: `test_delete_folder_removes_contents`, `test_delete_file_then_repeat_returns_404` |
| Every failure uses the shared error body and agreed status | Fast: failure tests in `test_create_routes.py`, `test_read_routes.py`, `test_update_routes.py`, `test_delete_routes.py` and `test_path_validation.py`; **new** `test_unknown_route_uses_shared_error_body` for the app-level handlers |
| Provider health reports Dropbox being down | **New** fast: `test_provider_health_is_503_when_dropbox_fails`; **new** live: `test_provider_health_is_ok` |

### Conformance suite

- **How it runs against us:** the suite is one test package in the unified repo. It runs against an in-memory reference backend on every PR there. Before each release we run it against our deployed service, with real Dropbox, by pointing it at our base URL.
- **Fixtures:** each test creates its own uniquely named folder and deletes it afterwards, so runs don't interfere and the account is left clean.
- **Consumer workflow it ends with**, unchanged across all three services: upload → metadata → download and compare bytes → list the folder → move → metadata at the new path → delete → metadata returns `404`.
- **What would reveal a mismatch:** if we still sent `modified` instead of `modified_at`, or returned the list unsorted, the workflow's field and order checks would fail.

### Concerns and gaps

- **Deferred: no conformance suite yet.** The unified repo's only tag, `0.1.0`, holds the mail-client template and no storage tests. Owner: Team 6 (Prateek), needed by Milestone 3. Until then, "matches the other services" is checked only by reading the memo.
- **Accepted: the 150 MB upload limit** isn't tested against real Dropbox; uploading such a file in every run is too slow and costly. We test our size check with a mock.
- **Accepted: a folder delete that fails partway** can only be simulated with a mock; we can't make Dropbox fail on demand.
- **Unverified:** what Dropbox returns for a stale revision on replace. The new live test above will confirm it before the replace PR merges.

## Open questions and review

Questions that affect the shared contract must close before we treat the contract as accepted. Local questions affect only Team 3, and we settle them during implementation. "Memo Q" numbers refer to the open questions in the Oct 28 memo.

### Open questions

| # | Question | Consequence for Team 3 | Owner | Next step | Needed by |
|---|---|---|---|---|---|
| 1 | The only tag, `0.1.0` (Nov 4), contains the mail-client template, not the storage contract (memo Q11) | We can pin a tag, but not one with the storage models and errors, so our Milestone 2 dependency is incomplete. The package targets Python 3.12 (memo Q10), and we run 3.11 | Dayana (coordinator) | Shreyas asks in the Document Storage channel for the storage contract to be committed and tagged | Nov 4; until then we report the dependency as incomplete |
| 2 | 409 or 422 for an occupied destination or a stale replace (memo Q3) | Changes create, replace and move responses and their tests | Darsh (Team 6), shared-repo issue #4 | Team 3 supports keeping 409, which all three services return today, on issue #4 | First contract tag |
| 3 | One code name per status (memo, Errors) | Renames codes in `errors.py`; the codes we rename appear on 43 lines in 5 route test files | Darsh (Team 6), issue #4; John and Victor implement | Adopt the issue #4 table once it is confirmed | First contract tag |
| 4 | Team 6's two rules: 404 means only a missing file, and a 502 or 503 after a write means the outcome is unknown (memo Q8) | Decision 3 relies on the first rule; delete's "check before retrying" relies on the second | Darsh (Team 6) | Team 3 supports both rules on issue #4 | First contract tag |
| 5 | Status for a folder delete that fails partway (memo Q6) | Fixes what delete's 502 means. Dropbox deletes a folder in one `files/delete_v2` call; its documentation does not say whether a failed call can leave the folder partly deleted | John (memo action "Recursive delete") | Write the delete clause in the unified repo: 204, 404 `not_found`, and 502 `provider_error` with "check before retrying" | First contract tag |
| 6 | What deleting the root returns (not in the memo) | Paths may omit the leading `/`, so `?path=/` names the root. A recursive delete there would empty the whole app folder. We reject it today with 422 | John, with Laya (Team 1, path rule) | Add to the delete clause: the root is never deleted, and the request returns 422 `invalid_operation` (issue #4 meaning) | First contract tag |
| 7 | Replace can recreate a file that is deleted between its two calls (Decision 1) | The memo's "replace never creates" fails in that window | Zesan | Tell Team 1 (Annie, Shini), who plan the same two calls, and document the window in the contract | First contract tag |
| 8 | Dropbox's maximum path length (memo, Paths) | The shared limit is the smallest of the three, and we cannot state ours | Victor | Find Dropbox's documented limit, or measure it with a live test | First contract tag |
| 9 | Upload limit unit and status (memo Q9) | Sets the size check and status in create and replace. Dropbox documents 150 MiB for one upload call (`files_upload` in Dropbox SDK 12.2.2) | Zesan, with Team 1 | Tell Teams 1 and 6 that the unit is MiB, and propose 413 `file_too_large` (issue #4) | Nov 8 (create and replace PR) |
| 10 | `modified_at` in UTC with a `Z` suffix (memo, data model) | Changes how we serialize Dropbox's timestamp | Jing | Confirm the UTC rule with the vertical | Nov 10 |
| 11 | What Dropbox returns for a stale rev with `autorename=False` (local) | Decides which exception replace raises | Zesan | Live test before PR #37 merges (see [Testing](#testing)) | Nov 8 |
| 12 | Keep `POST /files/rename` as a Team 3 extension (local) | One route and its tests | Team 3 | Settle in the design review | Nov 10 |

### Review

| Review | Reviewer | Scope | Status on Nov 4 |
|---|---|---|---|
| Whole design | A Team 3 member who did not write the section under review (to be named) | Every section | Not started |
| Contract-facing parts | A Team 1 or Team 6 member (to be named) | API and data model, failure behavior, migration, and the open questions above | Not started |

Each review states what the reviewer checked, for example "compared every route and status in this document with the memo and issue #4". Reviewers comment in this document or on the pull request that adds `DESIGN.md`.

Each substantive comment gets a disposition in the table below: a change with a link, an answer, or a reasoned decision to keep the approach. Resolving a comment thread alone is not a disposition.

| # | Comment and reviewer | Disposition | Link |
|---|---|---|---|
| — | No comments yet | — | — |

**Risks we propose to accept** (reviewers confirm or reject each one):

- Replace can recreate a file that is deleted between its two Dropbox calls (Decision 1, question 7).
- We do not test the 150 MiB upload limit against real Dropbox, and we simulate a partial folder delete only with a mock (see [Testing](#testing)).
- Dropbox treats paths that differ only in letter case as one file. The contract documents this, and we do not work around it.

**Scope of approval.** Approval accepts this plan at the reviewed revision. It does not show that the implementation is correct; the conformance suite and our live tests do that.

## Conclusion

This design proposes a shared HTTP contract for the common file storage operations used by Teams 1, 3, and 6. Consistent routes, file metadata, and error responses would allow consumers to use the same request and response handling across services, while each service retains its own provider implementation and credential configuration. The design documents the associated tradeoffs, including the additional Dropbox call proposed for replacement, the migration from existing routes, and the possibility that a provider failure leaves the outcome of a write uncertain.

Several contract and implementation details remain subject to review. Before the design is treated as accepted, the teams should resolve the outstanding questions about error codes, replacement behavior, and shared contract details; verify the relevant Dropbox behavior through live tests; and reconcile the proposed contract with the current implementation. The conformance suite and deployment checks can then provide evidence that the services meet the agreed contract consistently.
