# Runbook

What to do when the deployed service misbehaves. Each section starts from what you see, says how to find the cause, and how to know it's fixed. Commands assume `gcloud` is signed in to project `ospsd-team3` with at least Viewer access.

Services: `cloud-storage-service-test` (disposable) and `cloud-storage-service-demo` (the one graders use), region `us-central1`. How releases work and how to roll one back: [Release Pipeline](release-pipeline.md).

## Alert: callers are getting server errors (5xx)

**What it means:** one service returned more than 5 server errors (status 500–599) within 5 minutes. Callers' requests are failing; this is not a caller mistake (those are 4xx and don't count). The alert is defined in `infra/monitoring.tf` and emails the team when it opens and when it closes.

### 1. Find which errors, and since when

Open the alert's **Logs** link, or:

```bash
gcloud logging read 'resource.type="cloud_run_revision"
  resource.labels.service_name="cloud-storage-service-demo" httpRequest.status>=500' \
  --freshness=30m --limit=20 \
  --format="table(timestamp,httpRequest.status,httpRequest.requestMethod,httpRequest.requestUrl,resource.labels.revision_name)"
```

Then match the status to its cause. Our error bodies say which it is (`{"error": {"code": ...}}`):

| Status / `code` | Likely cause | Go to |
|---|---|---|
| `503 service_busy` | More requests in flight than `MAX_IN_FLIGHT_REQUESTS` (10 per instance) | [Overload](#overload-503-service_busy) |
| `503 provider_unavailable` | Dropbox unreachable | [Dropbox down](#dropbox-unreachable-503-provider_unavailable) |
| `503 provider_unavailable` in bursts while Dropbox's status page is green | Dropbox rate limiting us | [Rate limited](#dropbox-rate-limits-us-503-provider_unavailable-in-bursts) |
| `502 provider_error` on every request, `/health/provider` failing | Dropbox rejects our credentials | [Credentials](#dropbox-rejects-our-credentials-502-provider_error) |
| `502 provider_error` only on uploads, replaces and copies; reads work | The Dropbox account is out of space | [Storage full](#dropbox-storage-is-full-502-provider_error-on-writes-only) |
| `500 internal_error`, or errors starting with a new revision | A bug in a release | [Bad release](#a-bad-release) |

### 2. Check what is running and whether Dropbox is reachable

```bash
gcloud run services describe cloud-storage-service-demo --region=us-central1 \
  --format="table(status.traffic[].tag,status.traffic[].percent)"
curl -s https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/health/provider
```

`/health/provider` answers `{"status":"ok"}` when Dropbox accepts our credentials.

### Overload (`503 service_busy`)

**Seen in:** the dashboard's Saturation panel at the limit with refused requests, and the Errors panel's `unmatched` / `service_busy` series.

The service is protecting itself: requests beyond the limit are refused at once instead of queueing. It recovers by itself when the burst ends, and retrying after `Retry-After: 1` is always safe. If it lasts, find the caller in the logs (`httpRequest.remoteIp`). Raising the limit is a change to `MAX_IN_FLIGHT_REQUESTS` in `infra/run.tf`; see [Overload Protection](overload-protection.md) before doing it.

**Recovery can't undo:** nothing to undo. A refused request did nothing, so the caller only has to send it again.

### Dropbox unreachable (`503 provider_unavailable`)

**Seen in:** the Errors panel (`provider_unavailable` on every operation), failing `GET /health/provider`, and the synthetic probe failing from every checker. Traces show the Dropbox span failing or timing out.

Check <https://status.dropbox.com>. Nothing to fix on our side; the service keeps running and `/health` stays `200`, so Cloud Run won't restart it. It recovers when Dropbox does.

**Recovery can't undo:** writes that failed with 503 may or may not have happened (see [Fault Injection](fault-injection.md), `lost_response`). Callers should check metadata before retrying a write.

### Dropbox rate limits us (`503 provider_unavailable`, in bursts)

**Seen in:** `provider_unavailable` in the Errors panel that rises and falls with the Traffic panel, while <https://status.dropbox.com> is green and `/health/provider` mostly answers `200`. For Dropbox's general rate limit the log line's `reason` ends in `(RateLimitError)`; a burst of writes to one folder (`too_many_write_operations`) shows as a `WriteError` there instead.

Our side already bounds the rate: at most 2 instances × 10 requests in flight, so at most 20 Dropbox calls at once per environment. If it lasts, find the caller sending the burst (`httpRequest.remoteIp` in the logs). Lowering `MAX_IN_FLIGHT_REQUESTS` in `infra/run.tf` turns some of these 503s into faster `service_busy` refusals that never reach Dropbox. It recovers by itself when the burst ends.

**Recovery can't undo:** as for an outage, a failed write may have happened; check metadata before retrying.

### Dropbox storage is full (`502 provider_error` on writes only)

**Seen in:** the Errors panel shows `provider_error` on create, replace and copy, while metadata, list, download, move and delete still succeed. The log lines are `provider_failure: true`, and the Dropbox span returns an error.

Check the storage used by that environment's Dropbox account. Free space by deleting what the service's own tests may have left behind (folders named `/conformance-…`, `/workflow-…`, `/slo-run-…`) in that environment's app folder; never delete other files without asking their owner.

**Recovery can't undo:** the failed uploads were not stored. Callers have to send them again.

### Dropbox rejects our credentials (`502 provider_error`)

**Seen in:** `provider_error` on every operation in the Errors panel, `GET /health/provider` failing, and the synthetic probe failing. The trace shows the failing call is `POST https://api.dropboxapi.com/oauth2/token` (see [Telemetry Walkthrough](telemetry-walkthrough.md#3-a-provider-failure)).

Most often the refresh token was revoked or the Dropbox app's secret changed. Log in again to that environment's Dropbox app, add the new token as a new secret version, then deploy a new revision so it is read (secrets are read when an instance starts):

```bash
uv run python -m dropbox_client_impl login
uv run python -c 'import json; print(json.load(open(".dropbox_token.json"))["refresh_token"], end="")' \
  | gcloud secrets versions add dropbox-refresh-token-demo --data-file=-
gcloud run services update cloud-storage-service-demo --region=us-central1 --update-labels=secret-rotated=$(date +%s)
```

Then disable the old version (`gcloud secrets versions disable <n> --secret=dropbox-refresh-token-demo`).

**Recovery can't undo:** requests that failed while the token was rejected did nothing, so callers send them again. If the token was rejected because it leaked, rotating it stops further use but can't undo what was read or changed with it.

### A bad release

**Seen in:** errors starting when a new release took traffic: the dashboard's "Requests by release" panel changes colour at the same moment the Errors panel rises; every log line carries `release`. The pipeline's smoke check stops most bad releases before they get traffic (see [Rollback Drill](rollback-drill.md)).

If errors began when a new revision took traffic, roll back first and investigate after: see "Roll back" in [Release Pipeline](release-pipeline.md). Rollback restores code and configuration, not files a faulty release already changed in Dropbox.

### 3. Confirm it's fixed

- the alert email says **closed** (it closes once 5 minutes pass with 5 or fewer server errors);
- `curl -s <service url>/health/provider` returns `{"status":"ok"}`;
- the smoke check passes: `uv run --no-project --with httpx python scripts/consumer_workflow.py <service url>`.

## Alert test record

| Date | Environment | How it was triggered | Opened (email received) | Closed | Notes |
|---|---|---|---|---|---|
| 2026-11-11 | `test` | 8 bursts of 60 concurrent `GET /files/metadata` (20:33:54–20:35:27 UTC) with `BASE_URL=<test url> uv run python scripts/overload_demo.py /alert-drill/does-not-exist.txt 60 60`, run 4 times 20 s apart. 301 requests got `503 service_busy`; Cloud Monitoring counted 221 + 80 5xx in the minutes ending 20:35 and 20:36, then 0. | Yes: "opened" email received at the alert address | Yes: "closed" email received after the errors stopped | Read-only requests for a missing file, so no Dropbox data changed. The other 189 requests were `404` (caller errors, not counted). No action needed: the limiter recovers when the burst ends. |
