# Fault Injection

This page covers the HW2 optional extension **Fault injection**: a controlled way to make the storage provider slow or failing, the shared conformance suite run under each fault, and which of our guarantees held and which didn't.

## The contract and its limits

**Where faults are injected.** [`faults.py`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/src/cloud_storage_service/src/cloud_storage_service/faults.py) wraps the storage client in a `FaultInjectingClient` when the app starts. Every `Client` method goes through it (a unit test checks all 12). It fails the way a real provider does: it raises the built-in exceptions the `Client` contract documents, so the routes, error handlers and telemetry handle it exactly as they would a real Dropbox failure.

**The faults** (`FAULT_INJECTION`):

| Value | A provider call… | The caller sees |
|---|---|---|
| `latency=<ms>` | waits, then runs | the normal answer, later |
| `unavailable` | fails without reaching the provider (`ConnectionError`) | `503 provider_unavailable` |
| `error` | fails without reaching the provider (`OSError`) | `502 provider_error` |
| `timeout=<ms>` | waits, then fails as unreachable | `503`, after the wait |
| `lost_response` | **runs, then its reply is lost** | `503`, though the change was made |

Two options narrow a fault: `FAULT_OPERATIONS` (only these `Client` methods, e.g. `download_file`) and `FAULT_RATE` (the share of calls that fail, 0 to 1).

**Off by default, never on demo.**

- Nothing is injected unless `FAULT_INJECTION` is set. OpenTofu never sets it; it would only ever be set by hand on a test deployment.
- If it is set on `cloud-storage-service-demo` (Cloud Run's `K_SERVICE`), the service **refuses to start**.
- A malformed value (unknown fault, missing or out-of-range delay, unknown operation, rate outside 0–1) also stops startup, so a typo can't quietly do nothing.

All three are unit-tested in `src/cloud_storage_service/tests/test_faults.py`.

**To use it on the test service** without affecting its users, put it on a revision that gets no traffic, then remove it:

```bash
gcloud run services update cloud-storage-service-test --region=us-central1 \
  --no-traffic --tag=faults --update-env-vars=FAULT_INJECTION=unavailable
# … call https://faults---cloud-storage-service-test-<hash>-uc.a.run.app …
gcloud run services update cloud-storage-service-test --region=us-central1 \
  --no-traffic --remove-env-vars=FAULT_INJECTION
gcloud run services update-traffic cloud-storage-service-test --region=us-central1 --remove-tags=faults
```

## The experiment

`scripts/fault_matrix.py` starts the real app on the in-memory provider (`scripts/serve_in_memory.py`, no network or credentials) once per fault, runs the shared conformance suite against it (Team 6's commit `b77617c`), and records which tests passed and every status and error code the service answered with, from its one log line per request.

```bash
uv run python scripts/fault_matrix.py /path/to/ospsd26-cloud-storage
```

Run on Nov 11, 2026 at 22:03 UTC:

| Fault | Conformance passed | Took | Error answers (status code × count) | 500s |
|---|---|---|---|---|
| none | 55/55 | 1.2 s | 400 invalid_request x16, 404 not_found x26, 404 route_not_found x1, 405 method_not_allowed x1, 409 conflict x2, 422 invalid_operation x3 | 0 |
| `latency=300` | **55/55** | 58.6 s | same as none | 0 |
| `unavailable` | 13/55 | 1.4 s | 400 invalid_request x12, 404 route_not_found x1, 405 method_not_allowed x1, **503 provider_unavailable x92** | 0 |
| `error` | 13/55 | 1.0 s | 400 invalid_request x12, 404 route_not_found x1, 405 method_not_allowed x1, **502 provider_error x92** | 0 |
| `timeout=1000` | 13/55 | 93.8 s | 400 invalid_request x12, 404 route_not_found x1, 405 method_not_allowed x1, **503 provider_unavailable x92** | 0 |
| `lost_response` | 16/55 | 1.0 s | 400 invalid_request x12, 404 not_found x15, 404 route_not_found x1, 405 method_not_allowed x1, 503 provider_unavailable x77 | 0 |
| `error`, `download_file` only | 46/55 | 1.0 s | as none, plus **502 provider_error x9** | 0 |
| `unavailable`, 30% of calls | 33/55 | 1.0 s | 400 invalid_request x15, 404 not_found x27, 404 route_not_found x1, 405 method_not_allowed x1, 422 invalid_operation x1, 503 provider_unavailable x34 | 0 |

The 30% row varies between runs (another run gave 35/55), because which calls fail is random.

## What held

- **The error model held under every fault: zero `500`s.** Every injected provider failure reached the caller as the shared error body with `502 provider_error` or `503 provider_unavailable` and the fixed message, never as an unexpected error or a leaked exception.
- **Slow is not wrong.** With 300 ms added to every provider call, all 55 checks still pass; only time grows (1.2 s → 58.6 s for the suite).
- **What doesn't need the provider keeps working.** The 13 checks that pass under a total outage are exactly the ones that never call it: request validation (`400`), unknown routes (`404 route_not_found`), wrong methods (`405`), and **`GET /health`, which stays `200` while `GET /health/provider` fails**. So Cloud Run's liveness probe wouldn't restart the service during a provider outage, while readiness reports it.
- **A fault in one operation stays in that operation.** With only `download_file` failing, the 9 failing checks are the ones that download (directly, or to verify a replace or move); creates, lists, moves and deletes all pass.
- **Partial failures are visible, never silent.** At 30% failure, each failed call is a `503`; no check failed with a wrong answer.

## What broke, and why

- **Under a total outage the contract can't be met.** The 42 failing checks fail where they first need the provider (most at setup, creating a file) with `502`/`503`. That is the intended behaviour: the service reports the provider's failure instead of guessing.
- **`lost_response` makes the uncertain write real.** Every provider call that succeeded, reads included, came back to the caller as `503`; only calls the provider itself rejected (`404 not_found` x15, for paths that really didn't exist) kept their real answer, because they fail before the reply is "lost". So callers saw failures for writes that had in fact been made: `test_lost_response_makes_the_write_but_reports_503` in `test_faults.py` shows the file exists after the `503`, and that retrying the create then gets `409 conflict`. This is what the contract warns about (`ConnectionError` means a write *may* have happened), and why the runbook says to check metadata before retrying a failed write. The experiment shows the warning is needed; the service can't remove the uncertainty.
- **A failing `download_file` hides "not found".** `test_download_missing_file` fails under the download-only fault because the provider error (502) is reported before the service could learn the file doesn't exist (404). Correct, since we can't know, but a consumer can't tell a missing file from a broken provider during that fault.

## What this establishes, and what it doesn't

- **It establishes** that our routes, error handling and status codes behave as the contract says when the provider is slow, down, failing or losing replies, for every operation the conformance suite covers.
- **It does not** test the Dropbox adapter's own translation of Dropbox's errors into these exceptions. Faults are injected above the adapter, on the in-memory provider. That translation is covered by the Dropbox unit tests (real SDK error objects) and the live integration tests.
- **It does not** simulate network-level faults (DNS, TLS, partial reads) or Dropbox's real rate limits.
- `timeout` is a sleep followed by an error; it says nothing about how the Dropbox SDK's own 30 s timeout behaves.
