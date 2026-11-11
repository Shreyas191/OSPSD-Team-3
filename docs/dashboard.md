# Dashboard

This page covers HW2 Milestone 3, Level 3, item 3.2: one dashboard showing the four golden signals for each shared operation, with the deployed release visible. The data behind it comes from item 3.1 (one JSON log line per request; see `cloud_storage_service/telemetry.py`).

**Where:** Cloud Monitoring → Dashboards → **Cloud storage service: golden signals**, or `tofu -chdir=infra output dashboard_url`. The 5xx alert (item 3.5) links to it.
**Defined in:** [`infra/dashboard.tf`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/infra/dashboard.tf), applied with the rest of the infrastructure. Change it there, not in the console: a repeated `tofu apply` would undo a console edit.
**Service:** one dashboard for both environments. The filter bar at the top picks `cloud-storage-service-test` (the default: every push to `hw2` deploys there) or `cloud-storage-service-demo`.

## Why Cloud Monitoring and not Grafana Cloud

The action item says Grafana Cloud, which was DESIGN.md's plan. Item 3.1 sends logs, metrics and traces to Google Cloud's own Logging, Monitoring and Trace instead. A dashboard in the same place reads them directly. A Grafana Cloud dashboard would need a second account and a Google service-account key for Grafana to read our metrics, and would add another thing to own and tear down, for the same charts. The handout leaves the observability backend to the team.

## Operations

Each chart has one series per operation, named after its route:

| Operation (route name) | Request |
|---|---|
| `create_file` | `POST /files` |
| `replace_file` | `PUT /files` |
| `get_file_metadata` | `GET /files/metadata` |
| `download_file` | `GET /files/download` |
| `list_folder` | `GET /files` |
| `update_entry` | `PATCH /files` (move or rename) |
| `delete_entry` | `DELETE /files` |

`unmatched` is a request that matched no route (`404 route_not_found`), or one the overload limit refused before it was routed (`503 service_busy`). Health checks (`/health`, `/health/provider`) are left out of the traffic, error and latency charts: Cloud Run's liveness probe calls `/health` every 30 seconds, and those calls aren't demand.

## Panels, and what each one changes

| Panel | Question it answers | What an operator does after seeing it |
|---|---|---|
| **Release serving requests** | Which commit is answering requests right now? | Compare it with the commit of the last pipeline run. If it isn't the one you expect, a rollback or a stuck deploy happened: check "Which version is running?" in [Release Pipeline](release-pipeline.md) before trusting anything else on the page. |
| **Requests by release** | Did a rollout or rollback happen, and when? | If errors or latency changed when the colour changed, the new release is the likely cause: roll back first ([Runbook: A bad release](runbook.md#a-bad-release)), investigate after. |
| **Traffic** | How much demand does each operation get? | Zero is not "healthy", it's "no data": run the smoke check or `GET /health/provider` before concluding anything. A sudden spike: look at Saturation next, because the service refuses work beyond its limit. |
| **Errors**, by operation and error code | Which operation fails, and whose fault is it? | 4xx codes (`invalid_request`, `not_found`, `conflict`, `invalid_operation`, `file_too_large`) are callers' mistakes: no action on our side unless one consumer suddenly sends many, which points to a contract misunderstanding. `provider_error` / `provider_unavailable`: Dropbox ([Runbook](runbook.md)). `internal_error`: our bug, roll back if it started with a release. `service_busy`: overload, see Saturation. |
| **Latency of successful requests** (p50, p95) | Are successes slower than usual, and for which operation? | One operation slower after a release: roll back. Every operation slower at once with no release: Dropbox is slow; open a trace from the log panel to see time spent in the Dropbox span. |
| **Latency of failed requests** (p50, p95) | Do failures fail fast, or do callers wait for them? | Fast failures (validation, `not_found`) are expected. Failures taking seconds mean callers wait on provider timeouts; check Dropbox's status and the traces. |
| **Saturation**: concurrent requests per instance (one line per revision), against the limit of 10, and requests refused | How close is each instance to `MAX_IN_FLIGHT_REQUESTS`? | Near 10 with refused bars (`503 service_busy`): demand exceeds what two instances serve. Find the caller in the logs; raising the limit or `max_instances` is a reviewed change in `infra/` ([Overload Protection](overload-protection.md)). Well below 10 with errors elsewhere: load is not the cause. |
| **Failed requests** (log lines) | What exactly failed? | Open the entry: it has the operation, status, error code, release and trace ID, and links to its trace. Health checks aren't traced, so their entries have no link. |

## The release is part of every request

Every log line carries `release`: the full commit SHA, which the image sets at build time. `cloud_storage/requests_by_release` counts the lines by release. So the release panels show which commit served the requests in the selected time range, and the time of a change shows when a rollout or rollback happened. Open a commit at `https://github.com/Shreyas191/OSPSD-Team-3/commit/<sha>`.

## When there is no traffic

The services scale to zero, and every chart counts requests, so with no requests the charts say "No data". That is deliberate: no data is never drawn as zero errors. Cloud Run's liveness probe only runs while an instance is up, so the release panels also go empty when the service is idle. To find out whether an idle service works, call `GET /health/provider` or run `scripts/consumer_workflow.py` against it, then look again.

## What the data is, and its limits

| Signal | Source | Labels |
|---|---|---|
| Traffic, errors | `logging.googleapis.com/user/cloud_storage/requests` (item 3.1) | `route`, `outcome` (`ok` or the error code) |
| Latency | `logging.googleapis.com/user/cloud_storage/request_latency` (item 3.1): the service's own time per request, in ms | `route`, `outcome` |
| Release | `logging.googleapis.com/user/cloud_storage/requests_by_release` (this item) | `release` |
| Saturation | `run.googleapis.com/container/max_request_concurrencies` (Cloud Run): each instance's highest number of requests at once in the minute, averaged over the revision's instances; plus `requests` with `outcome = service_busy` | none (the revision is a resource label) |

- **Labels are bounded.** Routes are names, never paths. Outcomes are `ok` or one of the 12 error codes in `errors.py`. A release is one value per deployed commit. No file path, ID or URL is a label, and each environment's service is a resource label, not a metric label.
- **Delay.** Log-based metrics appear a minute or two after the request.
- **No history before a metric exists.** A log-based metric counts lines from the moment it is created, so `requests_by_release` has data only from its first apply.
- **What latency includes.** It is the time the service spent, from when it started handling the request until it finished sending the response, including the Dropbox calls. It doesn't include Cloud Run's front end or the network to the caller. Those are visible in Cloud Run's own `request_latencies`, but not per operation.
- **Saturation counts refused requests too.** Cloud Run sends up to 20 requests to an instance; the service works on 10 and answers the rest with `503 service_busy` straight away. A line above 10 therefore means requests are being refused, which the bars on the right-hand axis confirm.
- **Saturation is an average, not a maximum.** Cloud Run reports each instance's peak as a histogram whose buckets are 2 wide above 10, so a percentile of it reads up to 2 high: exactly 10 would show as 11.98, as if requests were being refused. The mean is exact. With one instance (the usual case) the line is that instance's peak; with two, it is their average, so one busy instance next to an idle one reads lower. The refused bars count every refusal exactly.

## Changing it

Edit `infra/dashboard.tf`, then `tofu fmt`, `tofu validate`, and `tofu plan`, which should show only the dashboard changing. Apply after review. Removing the infrastructure ([Teardown](teardown.md)) removes the dashboard and the metrics with it.
