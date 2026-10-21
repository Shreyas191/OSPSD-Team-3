# Engineering Decision: Translate Provider Errors into Built-in Exceptions

**Decision:** `DropboxClient` catches every Dropbox SDK error inside `dropbox_client_impl` and raises one of Python's built-in exceptions instead. Each `Client` method documents which ones it raises. The HTTP service maps those exceptions to status codes in one table (`cloud_storage_service/errors.py`).

| Built-in exception | Meaning in the `Client` contract | HTTP response |
|---|---|---|
| `ValueError` | The request is invalid (bad path or name, folder moved into itself) | `422 invalid_input` |
| `FileNotFoundError` | Nothing exists at the path | `404 not_found` |
| `FileExistsError` | The destination is already taken | `409 conflict` |
| `PermissionError` | The provider does not allow this operation on this entry | `403 permission_denied` |
| `ConnectionError` | The provider is unreachable or busy; retry later | `503 provider_unavailable` |
| any other `OSError` | The provider failed | `502 provider_failure` |

## The problem and the constraints that mattered

A request crosses three layers: the FastAPI route, the provider-independent `Client` contract, and the Dropbox SDK. When an operation fails, the caller needs to tell invalid input, a missing resource, a permission denial and a provider failure apart, without reading our code or Dropbox's error messages. Every failure has to be translated somewhere.

The constraints:

1. **Provider types stay behind the boundary.** `cloud_storage_client_api` and `cloud_storage_service` must never import `dropbox` (see `AGENTS.md`). So Dropbox's `ApiError` cannot reach the routes.
2. **Five people implement operations in parallel.** Create, Read, Update and Delete each have a different owner. The error contract had to be something every owner could follow without a design meeting, and that a reviewer could check by reading one docstring.
3. **Tests must not need Dropbox.** The fake `FakeStorageClient` has to reproduce every failure the routes handle, so the error vocabulary has to be easy to raise from test code.
4. **Keep the error model small.** We wanted to add as few new types as possible and follow conventions Python developers already know.

## Alternatives we considered

| Option | How it works | Why we didn't choose it |
|---|---|---|
| **A. Let SDK errors propagate** | Routes catch `dropbox.exceptions.ApiError` and inspect its error tag | Breaks constraint 1: the service would import Dropbox and change whenever the SDK does. Tests would need to build SDK error objects. |
| **B. Our own exception hierarchy** | `StorageError` with `NotFound`, `Conflict`, `Denied`, `ProviderFailure` subclasses in `cloud_storage_client_api` | Clear and could carry structured fields, but it's a new vocabulary every owner has to learn, plus one more module to maintain. |
| **C. Result objects** | Methods return `Ok(file)` or `Err(kind, message)` instead of raising | Every caller has to unwrap every result. It doesn't fit FastAPI's exception-based flow, and it would have changed every method signature. |
| **D. Built-in exceptions (chosen)** | Raise the standard `OSError` family plus `ValueError`, documented per method | Python's own file APIs already use these names with these meanings, so they need no new types and are trivial to raise from the fake. |

## Evidence

**Observations** (things we tested or measured):

- **Tests:**
  - **Fake client:** the route tests use `FakeStorageClient`, which raises the same built-ins with no network or credentials, and they run in CI on every push. Writing the fake's failure cases took one line each, e.g. `raise FileExistsError(msg)`.
  - **Real Dropbox:** the integration tests send rename, move and delete through the HTTP API to a real Dropbox account, and check that Dropbox's own `not_found` and conflict errors come back as `404` and `409` in the shared error format.
- **Containment:** only `dropbox_client_impl` imports `dropbox`. Everything else uses only the `Client` contract and built-in exceptions.
- **Dropbox quirks:** the translation layer is where Dropbox's surprises were absorbed, without changing the contract. Each was found by testing against real Dropbox:
  - **Case-insensitive paths.** A rename that only changes letter case (`report.txt` → `Report.txt`) comes back from Dropbox as a conflict, so it is a `FileExistsError`.
  - **Moving onto its own path.** Dropbox reports this as `duplicated_or_nested_paths` rather than a conflict. We map it to `FileExistsError`.
  - **A folder moved into itself.** Dropbox reports this with that same `duplicated_or_nested_paths` error, not its dedicated `cant_move_folder_into_itself` error. Our real-Dropbox test caught the service answering `409 "Something already exists"`. The fix lives entirely in `DropboxClient.move`, which rejects a nested destination before calling Dropbox and raises `ValueError` (`422`). The contract and the routes didn't change.
- **Consistency:** delete (#26) and update (#27) were implemented by different people and report failures in the same format, because both only had to raise the documented built-ins.

**Assumptions** (not yet established):

- That built-ins are enough for a second provider. We have only implemented Dropbox.
- That callers don't need more detail than a status code and a message, e.g. which entry conflicted, or how long to wait before retrying.

## What we are giving up, and what remains uncertain

- **No structured error details.** A built-in exception carries only a message. We can't attach a retry-after value, the conflicting entry's ID, or the provider's request ID for support, except inside the message text.
- **Classification depends on order.** `FileExistsError`, `PermissionError` and `ConnectionError` all subclass `OSError`, so the mapping must check the specific types before the generic `OSError`. We hit this: until `FileExistsError → 409` was added to `errors.py`, a conflict on any route using the shared format would have been reported as `502 provider_failure`.
- **Some distinctions are lost.** Running out of space is reported as a generic `OSError` (`502`), together with Dropbox outages. A caller can't tell "your account is full" from "Dropbox is down" except from the logged reason.
- **`ConnectionError` doesn't say whether the write happened.** After a lost connection, a move may or may not have gone through. We document that retrying is safe (a repeat after success returns `404`), but the exception itself can't say which case happened.
- **Not every operation follows it yet.** Create and Read still return the older `{"detail": ...}` responses, and some of their failures (e.g. downloading a folder raises `IsADirectoryError`) have no mapping in `errors.py` yet.

## When we would revisit this decision

- **Structured details:** if callers need them (retry-after, conflicting IDs), we'd move to option B, a small exception hierarchy whose classes subclass the matching built-ins so existing code keeps working.
- **A second provider:** if its failures don't fit the built-ins, for example a provider that allows duplicate names, or one with partial-success batch operations.
- **More error kinds:** if the `errors.py` table grows past roughly ten rows, or two failures need the same exception but different HTTP responses (e.g. quota vs outage).
- **Async provider calls:** if the service moves to them and cancellation or timeouts need their own semantics.
