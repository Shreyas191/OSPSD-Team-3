# Overload Protection

**Extension:** overload protection. The service works on at most `MAX_IN_FLIGHT_REQUESTS` requests at once (default 10). Any request beyond that is answered immediately with `503 service_busy` instead of waiting in a queue, and never reaches Dropbox.

## Why it's needed

Every route is a regular (synchronous) function, so FastAPI runs each request on a worker thread from a pool of 40. Without a limit, a burst of requests is handled like this:

- the first 40 each hold a thread and make a Dropbox call at the same time;
- the rest wait in memory for a thread. There is no bound on how many can pile up, or on how long each waits, and every one of them eventually calls Dropbox, which rate-limits bursts.

A caller can't tell a slow request from a stuck one, and the service's memory and Dropbox usage grow with the burst size.

## How it works

`ConcurrencyLimitMiddleware` (`src/cloud_storage_service/src/cloud_storage_service/overload.py`) wraps the whole app and keeps a count of requests in flight:

```
request ──► in_flight < limit ? ──yes──► in_flight += 1 ─► route ─► Dropbox ─► response ─► in_flight -= 1
                 │
                 no
                 ▼
           503 service_busy (+ Retry-After: 1), logged; Dropbox is not called
```

- **Atomic check:** the check and the increment happen with no `await` in between, so on the server's single event loop two requests can't both take the last slot.
- **Slots always free up:** the decrement is in a `finally`, so a request that fails (404, 422, 502, …) gives its slot back just like one that succeeds.
- **Docs exempt:** the interactive docs (`/docs`, `/openapi.json`) are not counted, so Swagger UI still loads while the service is full.
- **Per process:** with several uvicorn workers, each enforces its own limit.

### What a rejected caller sees

```http
HTTP/1.1 503 Service Unavailable
Retry-After: 1

{"error": {"code": "service_busy", "message": "The service is handling too many requests; retry after a short wait"}}
```

`service_busy` is a different code from `provider_unavailable` (also `503`), so callers can tell "this service is full" from "Dropbox is down or rate limiting us". Nothing was done, so retrying is always safe.

The service logs it like every other failure, with `provider_failure=False` because Dropbox wasn't involved:

```text
Request failed: operation='GET /files/metadata' status=503 code='service_busy' provider_failure=False reason='4 requests in flight (limit 4)'
```

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MAX_IN_FLIGHT_REQUESTS` | `10` | Maximum requests handled at once by one process. Must be a positive whole number; any other value stops the service at startup, so a typo can't silently disable the limit. |

Set it in `.env` or the environment before starting `uvicorn`.

**The default of 10 is an assumption, not a measurement.** It is well below the 40 worker threads, and small enough that a burst doesn't become a burst of Dropbox calls. Tune it with the demo below.

## Demonstration (real Dropbox)

`scripts/overload_demo.py` sends bursts of simultaneous `GET /files/metadata` requests for one real file and summarises the results. Each `200` is one Dropbox call; a `503` never reaches Dropbox. Results from October 21, 2026, single uvicorn process on a laptop:

```bash
MAX_IN_FLIGHT_REQUESTS=4 uv run uvicorn cloud_storage_service.app:app
uv run python scripts/overload_demo.py /ospsd-overload-demo/report.txt 4 16 32 64
```

| Burst | `MAX_IN_FLIGHT_REQUESTS=4` | `MAX_IN_FLIGHT_REQUESTS=100` (effectively no limit) |
|---|---|---|
| 4 at once | 4 × 200 (median 333 ms) | 4 × 200 (median 294 ms) |
| 16 at once | 4 × 200 (124 ms), 12 × 503 (8 ms) | 16 × 200 (171 ms) |
| 32 at once | 4 × 200 (123 ms), 28 × 503 (11 ms) | 32 × 200 (202 ms) |
| 64 at once | 4 × 200 (117 ms), 60 × 503 (20 ms) | 64 × 200 (212 ms) |

**What this shows:**

- **Bounded:** with a limit of 4, exactly 4 requests reach Dropbox per burst, whether the burst is 4 or 64. Without it, all 64 call Dropbox at once, and 24 of them wait for one of the 40 threads first.
- **Predictable:** rejected requests are answered in about 8–20 ms. They don't wait in a queue, and they cost only a small JSON response.
- **Not a speed-up:** in this run Dropbox didn't rate-limit even 64 simultaneous requests, so without the limit everything succeeded with only modestly higher latency. The benefit here is bounded concurrency and fast, explicit rejection, not lower latency. A limit set too low turns away requests the service could have served, which is why the default needs tuning.

## Tests

`src/cloud_storage_service/tests/test_overload.py` runs requests through the real app and routes. A fake storage client blocks inside `rename` until the test releases it, so the test controls exactly how many requests are in flight, with no timing or sleeps. It checks:

- **Rejection:** with every slot taken, the next request gets `503 service_busy` with `Retry-After`, and the client is never called.
- **Recovery:** once the running requests finish, new requests succeed; failed requests (404) free their slot too.
- **Docs exempt:** `/docs` and `/openapi.json` still load while the service is full.
- **Logging:** the rejection is logged once, with `provider_failure=False`.
- **Configuration:** the app installs the middleware with the configured limit; valid values are read from the environment, and invalid ones stop startup.

**Defect check:** changing the comparison to `in_flight > limit` (an off-by-one that lets one extra request through) makes the rejection test fail. The real check passes all 14 tests.

## Limits of this design

- The limit is per process and in memory. Several workers or machines would need a shared counter (e.g. Redis) to enforce one global limit.
- It limits how many requests run at once, not how many arrive per second. A steady stream of fast requests is never rejected.
- All operations count the same. A large upload and a metadata lookup each take one slot.
