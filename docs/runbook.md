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
| `503 provider_unavailable` | Dropbox unreachable or rate limiting us | [Dropbox down](#dropbox-unreachable-503-provider_unavailable) |
| `502 provider_error` | Dropbox answered with an error, e.g. expired credentials | [Credentials](#dropbox-rejects-our-credentials-502-provider_error) |
| `500 internal_error`, or errors starting with a new revision | A bug in a release | [Bad release](#a-bad-release) |

### 2. Check what is running and whether Dropbox is reachable

```bash
gcloud run services describe cloud-storage-service-demo --region=us-central1 \
  --format="table(status.traffic[].tag,status.traffic[].percent)"
curl -s https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/health/provider
```

`/health/provider` answers `{"status":"ok"}` when Dropbox accepts our credentials.

### Overload (`503 service_busy`)

The service is protecting itself: requests beyond the limit are refused at once instead of queueing. It recovers by itself when the burst ends, and retrying after `Retry-After: 1` is always safe. If it lasts, find the caller in the logs (`httpRequest.remoteIp`). Raising the limit is a change to `MAX_IN_FLIGHT_REQUESTS` in `infra/run.tf`; see [Overload Protection](overload-protection.md) before doing it.

### Dropbox unreachable (`503 provider_unavailable`)

Check <https://status.dropbox.com>. Nothing to fix on our side; the service keeps running and `/health` stays `200`, so Cloud Run won't restart it. It recovers when Dropbox does. Writes that failed with 503 may or may not have happened: callers should check metadata before retrying.

### Dropbox rejects our credentials (`502 provider_error`)

Most often the refresh token was revoked or the Dropbox app's secret changed. Log in again to that environment's Dropbox app, add the new token as a new secret version, then deploy a new revision so it is read (secrets are read when an instance starts):

```bash
uv run python -m dropbox_client_impl login
uv run python -c 'import json; print(json.load(open(".dropbox_token.json"))["refresh_token"], end="")' \
  | gcloud secrets versions add dropbox-refresh-token-demo --data-file=-
gcloud run services update cloud-storage-service-demo --region=us-central1 --update-labels=secret-rotated=$(date +%s)
```

Then disable the old version (`gcloud secrets versions disable <n> --secret=dropbox-refresh-token-demo`).

### A bad release

If errors began when a new revision took traffic, roll back first and investigate after: see "Roll back" in [Release Pipeline](release-pipeline.md). Rollback restores code and configuration, not files a faulty release already changed in Dropbox.

### 3. Confirm it's fixed

- the alert email says **closed** (it closes once 5 minutes pass with 5 or fewer server errors);
- `curl -s <service url>/health/provider` returns `{"status":"ok"}`;
- the smoke check passes: `uv run --no-project --with httpx python scripts/consumer_workflow.py <service url>`.

## Alert test record

| Date | Environment | How it was triggered | Opened (email received) | Closed | Notes |
|---|---|---|---|---|---|
| — | — | — | — | — | Not run yet |
