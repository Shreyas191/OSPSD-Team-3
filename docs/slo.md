# Service Level Objectives

This page covers HW2 Milestone 3, Level 4, item 3.4. It defines two service level indicators (SLIs) for one representative operation, `GET /files/metadata`: an SLI is the share of eligible requests that were good. Each SLI gets a service level objective (SLO), a target over a window, and the error budget that target allows. The page reports both over a recorded run. What the team does when a budget is spent is the error budget policy (item 3.6).

**Status:** definitions and targets written on Nov 11, 2026, from the measurement below, and committed before the recorded run ([`1f24208`](https://github.com/Shreyas191/OSPSD-Team-3/commit/1f24208), 22:06:16 UTC, and pushed; the run started at 22:06:27), so the run can't have shaped them. Changing a definition later is a pull request to this page that says why; never after a failure, to improve the result.

## The operation: `GET /files/metadata`

- **Consumers depend on it.** It is how a consumer checks that a file exists, and its size and date, before downloading it. The shared consumer workflow calls it three times, including the final check that a deleted file returns 404.
- **Its latency means something.** It makes one Dropbox call (`files_get_metadata`) whatever the file's size. Upload and download times grow with the file, so one threshold wouldn't fit them.
- **It is read-only**, so a recorded run changes nothing in Dropbox apart from its own scratch folder.

## Where the SLIs are measured

At **Cloud Run's request log** (`run.googleapis.com/requests`): one entry for every request Cloud Run received for the service, with the status it sent (`httpRequest.status`) and how long it took (`httpRequest.latency`). This is the closest point to the caller we have. It also counts what our own request log line (item 3.1) can't see:

- **the wait for a cold start.** When a request wakes the service, the app isn't running yet, so its own `duration_ms` starts late. Cloud Run's latency includes the wait;
- **Cloud Run's own errors**, such as `429` when every allowed instance is busy, or a `5xx` when the container crashes or fails to start;
- **requests refused by the overload limit** (`503 service_busy`).

`scripts/slo_report.py` reads that log with `gcloud logging read` and prints counts only. Request URLs carry file paths in the query string, so the script never prints one.

## Which environment the SLOs cover

**Demo**, the persistent environment consumers use. Test is where the team breaks the service on purpose: the overload test (Nov 11, 20:34 UTC: 301 `503 service_busy` in about a minute), the rollback drill, the provider-failure walkthrough. The SLOs never covered test, which was decided here, not after a bad result.

When the run below was recorded, demo didn't run the service yet (it was deployed only from `main`), so the report for demo said "no data". The recorded run therefore ran on **test**, with the same image, settings and limits. Pushes to `hw2` now deploy demo too, after test passes its smoke check, and the same command reports on it.

## Availability SLI

| | |
|---|---|
| **Eligible** | Every `GET /files/metadata` request Cloud Run logs for the service, whatever answered it (our code, the overload limit or Cloud Run), **except** `400 invalid_request` and `403 permission_denied` |
| **Good** | Answered `200` (the metadata) or `404 not_found` (nothing at the path) |
| **Bad** | Every other eligible request: `500 internal_error`, `502 provider_error`, `503 provider_unavailable`, `503 service_busy`, and Cloud Run's own `429` and `5xx` |
| **SLI** | good / eligible |

- **Caller mistakes don't count.** A malformed request (`400`) or a path the caller may not read (`403`) would fail the same way against a perfectly healthy service. Counting them would let one buggy consumer spend our budget.
- **`404` is a correct answer.** "Nothing exists there" is what the contract promises, and the consumer workflow's last step relies on it.
- **A Dropbox outage counts against us.** Consumers can't tell Dropbox's failure from ours, and choosing Dropbox was our decision. The budget is what shows whether Dropbox is reliable enough for the promise.
- **Overload refusals count.** A caller refused with `service_busy` got no answer.
- **The pipeline's smoke check counts.** It runs the consumer workflow through the same path as any consumer. It also gives demo some traffic after every deploy.

## Latency SLI

| | |
|---|---|
| **Eligible** | The availability SLI's good requests: answered `200` or `404` |
| **Good** | Cloud Run's `httpRequest.latency` is at most **1,000 ms** |
| **SLI** | good / eligible |

Failures are left out because the availability SLI already counts them, and a fast error shouldn't make latency look better. The time includes any cold start: the caller waits for it.

## Targets, windows and error budgets

| SLO | Target | Window | Error budget for 1,000 eligible requests |
|---|---|---|---|
| Availability | 99% answered `200` or `404` | 7 days, rolling | 10 failed requests |
| Latency | 95% answered within 1,000 ms | 7 days, rolling | 50 slow requests |

The window is 7 days because the team works in weekly milestones, and a shorter window would hold too few requests at our traffic.

### The measurement behind the targets

Cloud Run's request log for test, Nov 11, 2026 (the service's first day on Cloud Run; demo had no requests):

- **562 metadata requests.** 484 were the overload test (20:33 to 20:38 UTC). The other 78 were normal use: smoke checks, the rollback drill and the telemetry walkthrough.
- **Warm latency** (74 normal requests answered `200` or `404`): p50 141 ms, p95 397 ms, max 588 ms. 73 of 74 finished within 500 ms, all 74 within 1,000 ms. The fastest took 105 ms, roughly the Dropbox call alone.
- **Cold starts.** The service scales to zero (`min_instance_count = 0` in `infra/run.tf`). The 12 requests that each woke a new instance took 3,070 to 6,701 ms, most of them about 6,100 ms. Instances that a deploy had already started answered their first request in a few milliseconds.
- **Failures.** Among the 75 normal eligible requests, the one failure was deliberate: the `502` from the walkthrough's provider-failure step.

### Why these targets

- **The consumer's need:** metadata is an interactive lookup, often made just before a download or while showing a file list. About 1 second is the commonly cited limit for a response to keep someone's train of thought (Jakob Nielsen, "Response Times: The 3 Important Limits").
- **1,000 ms** is 2.5 times the warm p95, so a slow minute at Dropbox doesn't spend the budget. Every cold start (3 seconds or more) is slow, which is right: the caller waited.
- **95%** allows one slow request in 20. At our traffic the slow ones are almost all cold starts, so this budget measures whether scaling to zero is still acceptable. If cold starts spend it, the team has a known lever: `min_instance_count = 1` on demo, an always-running instance that costs money (see [Operation Cost](operation-cost.md) before changing it). 99% would be broken by cold starts alone whenever fewer than 100 requests follow each one, and 100% is impossible while the service scales to zero.
- **99% availability** allows 10 failures in 1,000: a short Dropbox error spell, or a bad release that the smoke check stops and a rollback reverses (the rollback drill took 4 seconds, and the faulty revision served for about 15). 99.9% would allow 1 in 1,000, which a single short Dropbox incident would spend. With one provider, one region and at most two instances, we can't honestly promise more than Dropbox delivers.

### Low traffic and missing data

- **No requests is "no data", not 100%.** The report prints `NO DATA` and exits with status 2, and the dashboard shows "No data" the same way.
- **Fewer than 100 eligible requests:** one bad request moves the SLI by more than a percentage point, so the percentage isn't judged against the target. The report prints `LOW TRAFFIC`, and each bad request is looked at individually in the dashboard's failed-requests panel. There are few of them at that traffic.
- **The budget is a count.** It is stated for 1,000 eligible requests (10 failed, 50 slow) and computed for the window's actual traffic in every report.
- **Logs arrive late.** Entries can take a minute or two, so wait that long after a run before reporting it.

## What the SLIs cannot observe

- **Requests that never reach Cloud Run:** a DNS failure, the caller's network, or a TLS or Google front-end failure before Cloud Run logs the request. For those, Cloud Run has no log entry, so they are neither good nor bad.
- **Time outside Cloud Run:** the network between the caller and Google, and the caller's own processing. The latency is the time Cloud Run took to answer.
- **Wrong answers with a correct status:** a `200` with stale or wrong metadata, or a `404` for a file that exists, counts as good. The rollback drill's fault (`DELETE` answered `204` without deleting anything) would have looked healthy to both SLIs; only the smoke check, which compares results, caught it.
- **Valid requests we wrongly reject with `400`** are excluded instead of counted bad. The conformance suite is what guards our validation.
- **A caller that gives up first:** a caller with a short timeout sees a failure, but the log records the answer we eventually sent. If that answer took more than 1,000 ms, the latency SLI counts it as slow; the availability SLI doesn't count it as failed.
- **Other operations:** uploads, downloads, listings, moves and deletes have no SLO.

## How to report

```bash
uv run python scripts/slo_report.py --project <project-id> --service cloud-storage-service-demo --days 7
uv run python scripts/slo_report.py --project <project-id> --service cloud-storage-service-test \
    --start <UTC start> --end <UTC end>
```

It prints, for each SLI, the numerator, denominator and value against the target, and the error budget the window allows, has used and has left. Exit status 0 means both SLOs are met, 1 means one is not, and 2 means no data. It needs `roles/logging.viewer` on the project. If `gcloud` refuses with `USER_PROJECT_DENIED`, prefix the command with `CLOUDSDK_BILLING_QUOTA_PROJECT=LEGACY`.

A recorded run is made with `scripts/slo_run.py <service URL>`. It sends metadata requests one at a time, in a scratch folder that it deletes afterwards, and prints the window to report on.

## Recorded run

**When and where:** Nov 11, 2026, 22:06:27 to 22:10:01 UTC, on `cloud-storage-service-test`, revision `00024-zuz`, release `098ebe9`, one instance.
**How:** `scripts/slo_run.py` sent 300 metadata requests one at a time, half a second apart, in the scratch folder `/slo-run-9733ec387bd4`, which it deleted afterwards (`204`).
**Report:** `scripts/slo_report.py --service cloud-storage-service-test --start 2026-11-11T22:06:27Z --end 2026-11-11T22:10:01Z`, run two minutes after the run ended.

| | Availability | Latency |
|---|---|---|
| Logged requests | 300: `200` ×265, `404` ×21, `400` ×14 | |
| Eligible (denominator) | 286 (the 14 `400`s excluded) | 286 (answered `200` or `404`) |
| Good (numerator) | 286 answered `200` or `404` | 285 within 1,000 ms |
| Current value | **100.00%** (target 99%) | **99.65%** (target 95%) |
| Error budget for this traffic | 2.86 failed requests | 14.30 slow requests |
| Used | 0 | 1 |
| Remaining | 2.86 (100%) | 13.30 (93%) |
| For 1,000 eligible requests | 10 failed allowed | 50 slow allowed |

Both SLOs were met over the run. With 286 eligible requests it is above the 100-request floor, so the percentages count.

- **The client and the server agree.** The script counted the same statuses (`200` ×265, `404` ×21, `400` ×14) as Cloud Run logged, so no request was lost on the way.
- **Latency at Cloud Run:** p50 139 ms, p95 170 ms, p99 349 ms. The client measured p50 196 ms and p95 230 ms: the extra ~60 ms is mostly the network between the client and Google, which the SLI can't see.
- **The one slow request** (22:08:03, `200`) took 2,261 ms at Cloud Run. The service's own log line shows 2,259 ms inside the service, so it was neither a cold start nor Cloud Run; its trace (`4cefa2c766f8d9f9a99b62c724450d1b`) shows whether the Dropbox call took the time.
- **No cold start in this run.** The instance was still running from 11 minutes earlier, so the first request took 349 ms. Cold starts are in the measurement above, and on demo, with fewer requests, they will be a larger share of the slow ones.

A report on test for the 24 hours before the run shows why test isn't covered: the overload test's 301 `service_busy` refusals put availability at 45.97% (257 of 559), with the budget spent 50 times over. That was a deliberate experiment, which is what test is for.
