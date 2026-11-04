# DESIGN: Team 3's Dropbox service on the shared Document Storage contract

| | |
|---|---|
| **Team / vertical** | Team 3 · Document Storage (provider: Dropbox) |
| **Authors** | Shreyas Kaldate (draft), with Zesan Rahman (Create), Jing Qian (Read), Ashik John (Delete), Victor Wang |
| **Reviewers** | Whole design: _<Team 3 teammate>_ · Contract-facing parts: _<Team 1 or Team 6 reviewer>_ |
| **Status** | **Draft**. Becomes *In review* when the PR opens, and *Accepted for implementation* after both reviews. |
| **Last updated** | 2026-11-03 |
| **Shared contract** | [`Dayana20/ospsd26-cloud-storage`](https://github.com/Dayana20/ospsd26-cloud-storage). **Not yet tagged.** The repository is still empty as of 2026-11-03 (see [Open questions](#9-open-questions-and-review), Q1). |
| **Decision memo** | *One API, Three Storage Services*, Document Storage decision memo, 2026-10-28 (provisional) |
| **Current service** | [`v0.1.0`](https://github.com/Shreyas191/OSPSD-Team-3/releases/tag/v0.1.0), HW1 final release |
| **Implementation work** | Team 3 issues for the contract changes (see [§7](#7-migration-and-coordination-plan)); upload create/replace: #36 / PR #37 |

The decision memo records what the vertical agreed. This document explains how Team 3's service will meet that agreement: what changes in our code, what we considered, how failures behave, and how we will verify it. Where the memo leaves a question open, this document says which choice we're assuming and what happens if the group decides otherwise.

---

## 1. Context, goals and non-goals

### What a consumer is trying to do
A consumer, for example a Calendar or Issue Tracker service that attaches files, wants to upload a file, read its metadata, download it, list a folder, move it, and delete it. It should be able to point at any of the three Document Storage services by changing only the base URL and credentials.

### Why that fails today
The same intent needs different code against each service, and some differences fail **silently**:

- **Upload** is `POST /files/upload` with `remote_path` and an `overwrite` flag on our service, but `POST /files` with `path` on Team 1's. A consumer written for Team 1 that sends `path` to us gets `422`. Worse, our `overwrite=true` silently replaces a file, while Team 1 never overwrites, so code tested against Team 1 can lose data on ours without any error.
- **Move** is `POST /files/move` with `{from_path, to_path}` here, and `PATCH /files?path=` with `{new_path}` on Team 1.
- **Metadata:** we return `is_folder` and a `modified` time **without a timezone**; Team 1 returns `modified_at` with `Z`. A consumer comparing times across services can be off by the server's UTC offset without noticing.
- **Bad input** is `422` on our service and `400` on Teams 1 and 6, so a consumer branching on `400` never sees our validation errors.
- **List** is `GET /folders/list` on our service. Team 1 has no list, and Team 6's is paged with a cursor.

### Goals (observable)
1. A consumer completes the vertical's common workflow against our service using the **shared routes, request shapes, file-info object and error body**, with no Team-3-specific code: upload → metadata → download (bytes compared) → list → move → metadata at the new path → delete → metadata returns `404`.
2. Every operation in the shared contract passes the vertical's **conformance suite** against our service backed by real Dropbox (Milestone 3).
3. Every failure a consumer can act on returns the shared error body with the agreed status and code. No raw Dropbox errors or untranslated `500`s, keeping what HW1 Level 5 achieved.

### Non-goals
- **Moving data between providers.** A path or `id` refers to a file in our Dropbox app folder only.
- **Hiding Dropbox's case-insensitivity.** `/A.pdf` and `/a.pdf` are the same file on Dropbox. The contract tells consumers not to rely on case-only differences, and we don't emulate S3's behaviour.
- **Supporting every Dropbox capability through the shared API.** Copy, create folder, search and rename stay as documented Team 3 extensions (see [§4.4](#44-how-hw1-operations-map-to-the-shared-api)).
- **Changing providers at runtime.** The provider is chosen once, at startup, as in HW1.

---

## 2. Proposed architecture and request flow

The HW1 layering stays. What changes is the shape of the HTTP edge, the shared models, and the error codes:

```
HTTP caller
   │  shared routes, e.g. PATCH /files?path=…        (cloud_storage_service: app.py)
   ▼
FastAPI route ── path normalization + request validation   (new: shared path rule, 400 for bad input)
   │  calls the abstract Client only
   ▼
Client contract (cloud_storage_client_api)  ◄── to be aligned with the shared Python package
   │
   ▼
DropboxClient (dropbox_client_impl)  ── the ONLY code that imports the Dropbox SDK
   │  translates SDK results → shared file info, SDK errors → built-in exceptions
   ▼
Dropbox API
   ▲
errors.py (cloud_storage_service) ── maps built-in exceptions → shared error body + status
```

| Component | Responsibility | Changes for the contract |
|---|---|---|
| **Shared contract package** (unified repo) | Shared Python models, errors and operation signatures (Python 3.12); no provider SDK | **New dependency**, pinned to a tag (Q1) |
| `cloud_storage_client_api` | Our provider-independent `Client`/`File` contract | Aligned with the shared package: `File` gains `modified_at` with a timezone; `is_folder` leaves the public model but stays internal, for list's `type` |
| `dropbox_client_impl` | Dropbox SDK calls, metadata → `File`, SDK errors → built-in exceptions | New `replace()` (`WriteMode.update(rev)`), a provider health check, and timezone-aware `modified_at`. Error translation stays here |
| `cloud_storage_service/app.py` | HTTP routes, request models, response models | New routes (`POST`/`PUT /files`, `PATCH /files`, `GET /files`, `/health`, `/health/provider`); path normalization; `400` for validation errors |
| `cloud_storage_service/errors.py` | One table from built-in exceptions to status and code | Codes renamed to the agreed names (Q3); validation failures become `400` |
| `build_storage_client()` / lifespan | The one place a provider is chosen; the client is built once at startup | Unchanged |

**State and ownership:**
- **The Dropbox client:** created once in the FastAPI lifespan and held on `app.state`; routes receive it through `get_storage_client`. Nothing else creates or closes it.
- **Credentials:** read only by `dropbox_client_impl.auth`.
- **No state of our own:** the service stores nothing; Dropbox holds all of it.

### One successful request: move
`PATCH /files?path=/Docs/report.pdf` with body `{"new_path": "/Archive/report.pdf"}`

1. **FastAPI** parses the query and body. The path rule normalizes both paths (adds a leading `/` if missing) and rejects empty paths and `.`/`..` segments with `400` before Dropbox is called.
2. **The route** `move_file` calls `client.move("/Docs/report.pdf", "/Archive/report.pdf")`. It knows only the abstract `Client`.
3. **`DropboxClient.move`** rejects a destination inside the source locally (folder into itself), then makes **one** call: `files_move_v2(from, to, autorename=False)`.
4. **The result:** Dropbox returns `RelocationResult.metadata`, which `DropboxFile` wraps. The route builds the shared file-info response: `200` with `{"id": "id:…", "name": "report.pdf", "path": "/Archive/report.pdf", "size": 1234, "modified_at": "…Z"}`. The `id` is unchanged by the move.

### One expected failure: move onto a taken destination
Same request, but `/Archive/report.pdf` already exists.

1. **Dropbox's error:** steps 1–3 as above. `files_move_v2` raises `ApiError` with `RelocationError.to(WriteError.conflict)`.
2. **Translation:** `DropboxClient.move` turns it into `FileExistsError("Something already exists at '/Archive/report.pdf'")`, so no SDK type leaves the package.
3. **The response:** `SharedErrorRoute` (in `errors.py`) maps `FileExistsError` to `409` (or `422`, per Q2), with the shared body `{"error": {"code": "conflict", "message": "…"}}`.
4. **Logging:** one structured warning is logged (`operation`, `status`, `code`, `provider_failure=False`, `reason`), with no credentials or request body.

**Where things happen:**
- **Validation and path normalization:** in the route's request models, before the client is called.
- **Domain conversion and error translation:** inside `DropboxClient`.
- **Choosing the HTTP status:** in `errors.py`, which also logs the failure.

---

## 3. Deliberate limits

- **Folder delete isn't all-or-nothing.** Dropbox deletes a folder in one call, but a failure after Dropbox acted leaves the outcome uncertain. We return `502` and say "check before retrying" rather than claim atomicity.
- **Large files:** uploads go to Dropbox in **one request**, which Dropbox limits to 150 MB; the agreed limit and its status are still open (Q5). Upload and download bodies are held in memory.
- **No paging toward the caller:** list returns the whole folder in one response, as the memo agreed. Memory and the number of Dropbox calls grow with folder size (see [§6.4](#64-cost-of-an-affected-operation)).

---

## 4. API and data model

The authoritative HTTP and Python contracts will live in the unified repository. Until it's tagged, this section follows the decision memo, and marks each choice the memo leaves open.

### 4.1 Routes we will expose

| Operation | Shared route | Success | Our failures |
|---|---|---|---|
| Health | `GET /health` | `200 {"status":"ok"}` | — |
| Provider health | `GET /health/provider` | `200 {"status":"ok"}` | `503` (Dropbox unreachable or credentials missing) |
| Create | `POST /files` (multipart: `file`, `path`) | `201` file info | `409`/`422` exists (Q2), `400` bad input, size limit (Q5) |
| Replace | `PUT /files` (multipart: `file`, `path`) | `200` file info | `404` missing, conflict on a concurrent change (Q2), `400` |
| Metadata | `GET /files/metadata?path=` | `200` file info | `404`, `400` |
| Download | `GET /files/download?path=` (route to confirm, Q4) | `200` raw bytes, `application/octet-stream` | `404`, `400`/`422` for a folder |
| List | `GET /files?path=<folder>` | `200` array, sorted by path | `404`, `400` |
| Move | `PATCH /files?path=<from>`, body `{"new_path": "<to>"}` | `200` file info at the new path | `404`, `409`/`422` taken (Q2), `400` |
| Delete | `DELETE /files?path=` | `204`, no body | `404`, `502` partial folder delete |

### 4.2 The file-info object

| Field | Type | Our source | Notes |
|---|---|---|---|
| `id` | string | Dropbox `metadata.id` (`id:…`) | Opaque. **Stable across move and rename, and across overwrite** (verified in HW1: `test_upload_then_overwrite_round_trip`, `test_rename_then_move_round_trip`). A copy gets a new `id`. |
| `name` | string | `metadata.name` | Last path segment. |
| `path` | string | `metadata.path_display` | Canonical absolute path starting with `/`. Dropbox returns its display casing, which can differ in case from the request. |
| `size` | integer | `FileMetadata.size` | Bytes. For folders in a list entry: `null` (Q6). |
| `modified_at` | string, ISO 8601 | `FileMetadata.server_modified` | Dropbox's SDK returns a naive UTC datetime. **We will send it with a `Z` suffix** (e.g. `2026-10-23T12:00:00Z`), assuming the UTC rule is adopted (Q7). Folders: `null`. |
| `type` | `"file"` \| `"folder"` | `isinstance(metadata, FolderMetadata)` | **List entries only**, per the memo. |

### 4.3 Representative request and response (illustrative, not yet observed)

```http
PATCH /files?path=/Docs/report.pdf
Content-Type: application/json

{"new_path": "/Archive/report.pdf"}
```
```http
HTTP/1.1 200 OK
Content-Type: application/json

{"id": "id:AbC123", "name": "report.pdf", "path": "/Archive/report.pdf", "size": 1234, "modified_at": "2026-10-23T12:00:00Z"}
```

Once implemented, this will be replaced by a sanitized observed response and the command that produced it.

### 4.4 How HW1 operations map to the shared API

| HW1 (today) | Shared contract | What changes |
|---|---|---|
| `POST /files/upload` (`remote_path`, `overwrite`) | `POST /files` + `PUT /files` (`path`) | Split into create and replace; field renamed; `201` for create (#36, PR #37). The `overwrite` flag goes away. |
| `GET /files/metadata?path=` | same route | Response: drop `is_folder`; `modified` → `modified_at` with `Z`. |
| `GET /files/download?path=` | same route (Q4) | No change expected. |
| `GET /folders/list?path=` | `GET /files?path=` | New route; adds `type`; sorted by path; still fetches every Dropbox page. |
| `POST /files/move` (`from_path`, `to_path`) | `PATCH /files?path=` (`new_path`) | New method, route and body. |
| `DELETE /files?path=` | same route | Already recursive. Adds the `502` partial-failure meaning. |
| `POST /files/rename` | covered by move | **Extension**, kept (see §5 alternative B). |
| `POST /files/copy`, `POST /folders/create`, `GET /files/search` | not in the shared core | **Extensions**, unchanged and documented as Team 3-only. |
| — | `GET /health`, `GET /health/provider` | **New.** |

### 4.5 Translations that lose or narrow information
- **`is_folder` leaves metadata.** `GET /files/metadata` will no longer say whether a path is a folder. Consumers can tell from `size` and `modified_at` being `null`, or from list's `type`. We record this removal here because HW2 doesn't allow a feature to disappear silently.
- **Case-insensitive paths.** A case-only rename (`report.txt` → `Report.txt`) is a conflict on Dropbox. We document this; we don't work around it.
- **The `overwrite` flag is replaced by two verbs.** Behaviour is preserved (`PUT` replaces), but one call becomes two Dropbox calls (`files_get_metadata` + `files_upload` with `WriteMode.update(rev)`), so a concurrent change is refused instead of silently overwritten.

---

## 5. Alternatives and tradeoffs

### Decision A: replace the HW1 routes, or keep them as compatibility aliases

| | **Replace (chosen)** | Keep the old routes as aliases for one release |
|---|---|---|
| **Consumers** | Only the shared routes exist, matching the other two services | Old clients keep working |
| **Implementation** | Each route has one handler and one set of tests | Two routes per operation, doubled tests, and two error behaviours to keep in sync (the old routes answer `422` for bad input; the new ones `400`) |
| **Evidence** | No external consumer uses our HW1 routes yet. Callers are our tests, `TUTORIAL.md` and staff demos. The other verticals were told not to build against the contract until it's tagged | — |

**We give up** backward compatibility for HW1 callers. The change ships as `v0.2.0`, with the breaking change in the release notes, and `TUTORIAL.md` is recaptured.

**We would revisit** this if a consumer outside Team 3 is found depending on the HW1 routes before the release.

### Decision B: keep `POST /files/rename`, or fold it into move

`PATCH /files` with `new_path` in the same folder already renames. We **keep rename as a documented extension** because it costs nothing: one line delegating to `move`, with its tests already written. It also guarantees "same folder, new name", which the shared move doesn't. It's labelled Team 3-only.

**We would revisit** this if reviewers prefer a strictly minimal surface.

### Decision C: replace an existing file in one call or two

- **Option 1:** `files_upload(mode=WriteMode.overwrite)`, one Dropbox call. It overwrites whatever is there, including a change someone else made a moment earlier.
- **Option 2 (chosen, as Team 1 proposed):** read the current revision with `files_get_metadata`, then `files_upload(mode=WriteMode.update(rev))`. That's two calls; Dropbox refuses the write if the file changed in between. This also gives the `404` "nothing to replace" result for free from the first call.

**The cost:** one extra round trip per replace, and a window between the two calls in which the file can be deleted, giving `404` from the second call. **Unverified with real Dropbox:** what `WriteMode.update` returns for a stale revision. To be confirmed with an integration test before the PR merges.

### Decision D: keep error translation in the client, not the routes
Kept from HW1 (see [`docs/engineering-decision.md`](docs/engineering-decision.md)). The contract only renames codes and changes validation errors to `400`. Both are one-table changes in `errors.py`, because the routes don't catch exceptions themselves.

---

## 6. Failure behaviour and operational concerns

### 6.1 Provider outcome → domain error → HTTP

| Dropbox outcome (examples) | Built-in exception | HTTP (planned) |
|---|---|---|
| Path missing (`LookupError.not_found`) | `FileNotFoundError` | `404 not_found` |
| Destination taken (`WriteError.conflict`, `duplicated_or_nested_paths`) | `FileExistsError` | `409 conflict`, or `422` per Q2 |
| Malformed path, folder into itself, folder given to download, file given to list | `ValueError` / `IsADirectoryError` | `400` bad input, or `422` invalid operation (Q2/Q3) |
| No write permission, locked or shared-folder restriction | `PermissionError` | `403` |
| Rate limit, network error, timeout | `ConnectionError` | `503` |
| Quota, internal error, auth/5xx, unknown | `OSError` | `502` |
| Request validation (missing field, bad path) | — (FastAPI) | **`400`** (today `422`) |
| Over our concurrency limit (HW1 extension) | — | `503 service_busy` + `Retry-After` |

**Uncertain writes:** after a `502` or `503` from create, replace, move or delete, the write may or may not have happened. We don't retry automatically. The response says to check `GET /files/metadata` before retrying, which is the rule the memo proposes. Retrying is safe for move and delete (a repeat after success returns `404`), and for create (a repeat returns `409`).

### 6.2 Configuration, credentials and data exposure
- **Configuration:** `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, plus a refresh token from `.dropbox_token.json` or `DROPBOX_REFRESH_TOKEN`, and `MAX_IN_FLIGHT_REQUESTS`. These come from `.env` or the environment (CircleCI context in CI) and are never committed or built into an artifact.
- **Startup checks:** the service fails at startup with a clear message if credentials are missing; that behaviour exists since HW1.
- **Logs:** they record the operation, status, code and a sanitized reason, never tokens, request bodies or file contents. Provider details are logged but not returned in `502`/`503` bodies.

### 6.3 Limits and cleanup
- **Limits:** single upload 150 MB per Dropbox docs (unverified by experiment; Q5); shared maximum path length to confirm (Q8).
- **Cleanup:** integration tests and the conformance run use uniquely named scratch folders that are deleted afterwards. A test interrupted mid-run can leave a `/ospsd-it-…` folder behind, which the README cleanup section explains how to remove.

### 6.4 Cost of an affected operation
List changes route and adds sorting. The existing analysis in [`docs/operation-cost.md`](docs/operation-cost.md) still holds for its Dropbox calls (⌈n/P⌉ sequential calls) and memory (O(n)). Sorting by path adds **O(n log n)** local time, with no extra provider calls. Replace goes from 1 Dropbox call to 2 (§5 C). We don't infer Dropbox's internal costs from latency.

### 6.5 Hosting and tools for Milestone 3 (proposal, not yet chosen)

| Need | Proposal | Assumption to check |
|---|---|---|
| Container | Docker image built from `uv.lock` | — |
| Hosting | Google Cloud Run or Render | Free-tier limits and who owns the account |
| Infrastructure as Code | OpenTofu | State stored in a protected remote backend |
| Pipeline | CircleCI (existing) or GitHub Actions | Deploy credentials kept in a CI context |
| Telemetry | OpenTelemetry → Grafana Cloud | Free-tier quota |

The account owner, expected cost and teardown date will be recorded here once they're chosen.

---

## 7. Migration and coordination plan

### 7.1 Contract revision we will adopt
The first tag of `Dayana20/ospsd26-cloud-storage`. **It doesn't exist yet** (Q1). Until it does:
- we build against the decision memo;
- the shared package dependency is added the day the tag is cut;
- the Python version moves from 3.11 to 3.12, the shared package's version. That touches `.python-version`, every `pyproject.toml`, CircleCI images and `uv.lock`.

### 7.2 Compatibility for existing callers
All moved routes break HW1 callers (see §5 A), and validation errors change from `422` to `400`. This ships as `v0.2.0`, with the breaking changes listed. `TUTORIAL.md`, the README and the service README are updated in the same release.

### 7.3 Reviewable changes

| # | Change | Owner | Depends on | Verification |
|---|---|---|---|---|
| 1 | **Move → `PATCH /files`** (first end-to-end path) | Shreyas | memo; Q2 for the status | Route tests with the fake client + real-Dropbox integration test |
| 2 | Create/replace: `POST`/`PUT /files` (#36, PR #37) | Zesan | Q2, Q5 | Route tests + integration test incl. stale-revision replace |
| 3 | File info: `modified_at` with `Z`, drop `is_folder` | Jing | Q7 | Model tests + integration test on the timezone |
| 4 | List → `GET /files` with `type`, sorted | Jing | shared-repo issue #3 | Pagination tests (existing) + integration test |
| 5 | Delete: `502` on partial folder delete | Ashik | Q9 | Client error-table tests |
| 6 | Errors: `400` for validation, agreed code names | Ashik / Victor | shared-repo issue #4 (Q2, Q3) | Route tests across all operations |
| 7 | Paths: normalize; reject empty, `.`, `..` | Victor | Q8 (path rule) | `test_path_validation.py` rewritten |
| 8 | Health: `/health`, `/health/provider` | Victor | shared-repo issue #2 | Route tests + one live provider check |
| 9 | Adopt the tagged shared package; Python 3.12 | Shreyas | **Q1 (tag)** | Full suite + CI on 3.12 |
| 10 | Run the conformance suite against our service | Shreyas | Q1, Team 6 suite | Suite passes on real Dropbox; results recorded |
| 11 | Docs: extensions, `TUTORIAL.md`, READMEs | Zesan / Shreyas | 1–8 | Fresh-clone check |

**Early end-to-end step:** change 1 (move) runs against real Dropbox through the shared route first. It touches the request shape, the response model and the error body, which are the three things most likely to expose a wrong assumption while it's still cheap to fix.

### 7.4 If the contract changes incompatibly after we start
- **Pinning:** we pin to a tag, never a branch, so a change on the unified repo's `main` can't break us without our noticing.
- **Approval:** a breaking change needs a new tag and sign-off from all three implementation contacts (Jing Qian for Team 3), as the memo agreed.
- **Notification:** when one is proposed, the clause owner announces it in the Document Storage channel. We assess the affected issues in §7.3, update this document and our pinned tag in one PR, and notify Calendar or Issue Tracker teams that call us.

---

## 8. Testing

| Guarantee | How it's checked | Where |
|---|---|---|
| Each route accepts the shared request shape and returns the shared file info / error body | Route tests with `FakeStorageClient`, with no network | `src/cloud_storage_service/tests/` |
| Dropbox errors become the documented built-ins (no SDK type escapes) | Table-driven tests with a mocked SDK | `src/dropbox_client_impl/tests/` |
| Real Dropbox behaves as we assume: conflict on a taken path, identical re-upload refused, `id` kept across move and overwrite, folder into itself rejected | Integration tests in uniquely named scratch folders, deleted afterwards | `tests/integration/` (19 today) |
| **Replace refuses a concurrent change** (`WriteMode.update(rev)`) | **New integration test:** change the file between the two calls | `tests/integration/test_create_integration.py` |
| `modified_at` carries UTC | **New integration test** comparing it with a known upload time | `tests/integration/` |
| **Our service matches the other two** | **Shared conformance suite** (owner: Team 6, Prateek), run against our service and real Dropbox before every release, and against an in-memory reference on every PR in the unified repo | unified repo |

**The consumer workflow the suite ends with,** and which we'll run unchanged: upload → metadata → download and compare bytes → list the folder → move → metadata at the new path → delete → metadata returns `404`. If our list didn't sort, or our metadata still sent `modified` without a timezone, that workflow's assertions on list order or the `modified_at` format would fail, revealing the mismatch.

**Gaps:**
- **No pinned shared package and no conformance suite yet** (Q1). Until they exist, the "matches the other services" guarantee is only checked by reading the memo.
- **The 150 MB limit** is not tested against real Dropbox.
- **Partial folder-delete failures** can only be simulated with a mock.

---

## 9. Open questions and review

| # | Question | Consequence for us | Owner | Next step |
|---|---|---|---|---|
| Q1 | **No tagged contract yet** | We can't meet "depend on a tagged revision" for Milestone 2, and change 9 is blocked | Coordinator (Dayana) | Ask for the first tag in the Document Storage channel (Shreyas, 2026-11-03) |
| Q2 | `409` or `422` for an existing destination | Changes create, move and copy responses and their tests | Team 6 (Darsh), shared-repo issue #4 | Decide before tagging |
| Q3 | Code names (`invalid_request`/`provider_error` vs ours) | Renames codes in `errors.py` and every test | Team 6 (Darsh) | Same decision as Q2 |
| Q4 | Download route | None if it stays `GET /files/download?path=` | Clause owners | Name it in the contract draft |
| Q5 | Upload size limit (150 MB or MiB) and its status | Size check and status in create/replace | Teams 1 and 3 | Confirm against Dropbox docs and pick a status |
| Q6 | `size` / `modified_at` for folders in list | Model nullability | Jing (metadata clause) | Write it into the contract |
| Q7 | Timezone rule (`Z`) | Our `modified_at` serialization | Group | Confirm; we'll implement `Z` |
| Q8 | Path rule and shared maximum length | Replaces our `AbsolutePath` pattern | Team 1 (Laya) | We need to confirm Dropbox's path length limit |
| Q9 | Status for a folder delete that fails partway | Delete error mapping | All teams | Agree `502` + "check before retrying" |
| Q10 | Keep `POST /files/rename` as an extension? | Small: one route | Team 3 | Settle in review (§5 B) |

**Review:**
- **Whole design:** reviewed by _<Team 3 teammate>_.
- **Contract-facing parts** (§4, §6.1, §7.4): reviewed by _<Team 1 or Team 6>_, on the PR that adds this file.
- **Comments:** substantive ones are answered there, either with a change or with a reasoned reply.
- **Accepted risks:** listed here once agreed.

---

## 10. Next actions

| Action | Owner | Output | Depends on | Checkpoint |
|---|---|---|---|---|
| Open Team 3 issues for changes 1–11, with owners and dependencies | Shreyas | GitHub issues | — | 2026-11-03 |
| Request the first contract tag | Shreyas | Message in the vertical channel | — | 2026-11-03 |
| Implement move → `PATCH /files` against real Dropbox | Shreyas | PR + integration test | Q2 (provisional status) | 2026-11-04 (Milestone 2) |
| Get reviews of this design | Shreyas | PR comments | — | 2026-11-04 |
| Finish create/replace | Zesan | PR #37 | Q2, Q5 | 2026-11-08 |
| Adopt the tag, Python 3.12, run conformance | Shreyas | PR + recorded results | Q1 | 2026-11-10 |
| Complete changes 3–8 and 11 | Owners in §7.3 | PRs | Their questions | 2026-11-10 |
| Update this document wherever implementation changes the plan | Shreyas | Revision note below | — | Milestone 3 (2026-11-11) |

### Revision history
- **2026-11-03:** first draft (Shreyas), from the 2026-10-28 decision memo and the `v0.1.0` code.
