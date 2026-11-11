# Operational Runbook

What to do when the deployed service misbehaves. Each section starts from what you see, says how to triage and find the cause, how to mitigate the failure safely, and how to verify recovery. Commands assume `gcloud` is signed in to project `ospsd-team3` with at least Viewer access.

Services: `cloud-storage-service-test` (disposable) and `cloud-storage-service-demo` (the one graders and consumers use), region `us-central1`.
Related documentation: [Release Pipeline](release-pipeline.md), [Rollback Drill](rollback-drill.md), [Service Level Objectives](slo.md), and [Error Budget Policy](error-budget-policy.md).

---

## Alert: Callers Are Getting Server Errors (5xx)

**What it means:** one service returned more than 5 server errors (status 500–599) within 5 minutes. Callers' requests are failing; this is not a caller mistake (those are 4xx client errors and do not count). The alert is defined in `infra/monitoring.tf` and emails the team when it opens and when it closes.

### Initial Triage: Find which errors, and since when

Open the alert's **Logs** link in the notification email, or run:

```bash
gcloud logging read 'resource.type="cloud_run_revision"
  resource.labels.service_name="cloud-storage-service-demo" httpRequest.status>=500' \
  --freshness=30m --limit=20 \
  --format="table(timestamp,httpRequest.status,httpRequest.requestMethod,httpRequest.requestUrl,resource.labels.revision_name)"
```

Then inspect the JSON error body (`{"error": {"code": ...}}`) in Cloud Logging to identify the failure mode:

| Status / `code` | Top Failure Mode | Triage & Mitigation Section |
|---|---|---|
| `503 provider_unavailable` | External Provider Outage (Dropbox down/slow) | [1. Provider Outage](#failure-mode-1-provider-outage-503-provider_unavailable) |
| `502 provider_error` | Expired / Revoked Provider Credentials | [2. Expired or Invalid Credentials](#failure-mode-2-expired-or-invalid-credentials-502-provider_error) |
| `503 service_busy` | Quota / Concurrency Limit Exceeded | [3. Quota Exceeded / Concurrency Limit](#failure-mode-3-quota-exceeded--concurrency-limit-503-service_busy) |
| `500 internal_error` (or errors coinciding with a new revision) | Bad Release Deployed | [4. Bad Release](#failure-mode-4-bad-release-500-internal_error-or-regressions) |

Check current service state and provider reachability:

```bash
gcloud run services describe cloud-storage-service-demo --region=us-central1 \
  --format="table(status.traffic[].tag,status.traffic[].percent)"
curl -s https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/health
curl -s https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/health/provider
```

- `/health` answers `{"status":"ok"}` without calling Dropbox. If `/health` fails, the container or process has crashed.
- `/health/provider` calls `client.check_health()` against Dropbox and answers `{"status":"ok"}` when credentials and connectivity work.

---

## Failure Mode 1: Provider Outage (`503 provider_unavailable`)

### Symptom and Telemetry
- **Alert:** Server errors alert fires; dashboard shows a spike in `503` under operation series with error code `provider_unavailable`.
- **Logs:** Warning/error logs showing `provider_failure: true`, `code: "provider_unavailable"`, `reason: "The storage provider is unreachable or busy; try again later"`.
- **Traces:** Traces show the service span waiting on an outbound call (`POST https://api.dropboxapi.com/...`) that either times out or returns network connection errors.
- **Health Probes:** `GET /health` returns `200 {"status":"ok"}`. `GET /health/provider` returns `503 provider_unavailable`.

### Triage Steps
1. Verify Dropbox system status at <https://status.dropbox.com>.
2. Run a direct health probe against the deployed service:
   ```bash
   curl -s https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/health/provider
   ```
3. Check outbound latency in Google Cloud Trace to distinguish total dropouts from high provider latency.

### Safe Mitigation
1. **Do not restart or redeploy the service.** Our liveness probe uses `GET /health` (which does not hit Dropbox). Cloud Run will not restart healthy containers in a loop.
2. If Dropbox is experiencing an active incident, post a notice in vertical and shared coordination channels informing partner teams (Team 1, Team 6) of provider degradation.
3. If specific write operations are failing with 503, notify callers not to issue blind retries: write requests may have been committed by Dropbox before the connection dropped. Callers should check metadata before retrying.

### What Recovery Cannot Undo
Writes that timed out or returned 503 may have already been executed by Dropbox before the reply was lost. Rollbacks or retries cannot undo state changes already accepted by Dropbox.

### Recovery Verification
1. `GET /health/provider` returns `200 {"status":"ok"}`:
   ```bash
   curl -s https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/health/provider
   ```
2. Run the shared consumer workflow to confirm end-to-end functionality:
   ```bash
   uv run --no-project --with httpx python scripts/consumer_workflow.py https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app
   ```
3. Cloud Monitoring alert transitions from open to closed.

---

## Failure Mode 2: Expired or Invalid Credentials (`502 provider_error`)

### Symptom and Telemetry
- **Alert:** Server errors alert fires; dashboard shows immediate 100% failure rate for all provider-backed operations with `502 provider_error`.
- **Logs:** Error logs indicate `provider_failure: true`, `code: "provider_error"`, and reason containing `AuthError` or `invalid_grant` / `invalid token`.
- **Traces:** In Google Cloud Trace, the outbound span `POST https://api.dropboxapi.com/oauth2/token` returns HTTP `400` or `401`.
- **Health Probes:** `GET /health` returns `200`. `GET /health/provider` returns `502 provider_error`.

### Triage Steps
1. Check `/health/provider`:
   ```bash
   curl -s https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/health/provider
   ```
2. Inspect Cloud Logging for the specific auth error:
   ```bash
   gcloud logging read 'resource.labels.service_name="cloud-storage-service-demo"
     jsonPayload.code="provider_error"' --freshness=15m --limit=5
   ```
3. Check whether the refresh token was revoked in the Dropbox App Console or Secret Manager version changed unexpectedly.

### Safe Mitigation
1. Re-authenticate locally against the target environment's Dropbox App to generate a fresh refresh token:
   ```bash
   uv run python -m dropbox_client_impl login
   ```
2. Store the fresh refresh token as a new secret version in Google Secret Manager without printing it to console or shell history:
   ```bash
   uv run python -c 'import json; print(json.load(open(".dropbox_token.json"))["refresh_token"], end="")' \
     | gcloud secrets versions add dropbox-refresh-token-demo --data-file=-
   ```
3. Update the Cloud Run service to trigger a new revision that reads the new secret version:
   ```bash
   gcloud run services update cloud-storage-service-demo --region=us-central1 --update-labels=secret-rotated=$(date +%s)
   ```
4. Once the new revision is healthy, disable the expired secret version:
   ```bash
   gcloud secrets versions disable <old_version_number> --secret=dropbox-refresh-token-demo
   ```

### What Recovery Cannot Undo
Requests that failed with `502 provider_error` during the invalid token window were rejected before reaching Dropbox. No files or metadata were altered; callers must re-execute their operations.

### Recovery Verification
1. `GET /health/provider` answers `200 {"status":"ok"}`:
   ```bash
   curl -s https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/health/provider
   ```
2. Run the smoke check:
   ```bash
   uv run --no-project --with httpx python scripts/consumer_workflow.py https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app
   ```
3. Verify that the 5xx alert email closes.

---

## Failure Mode 3: Quota Exceeded / Concurrency Limit (`503 service_busy`)

### Symptom and Telemetry
- **Alert:** Alert triggers if more than 5 requests fail within 5 minutes; dashboard shows elevated 503 count under `service_busy`.
- **Logs:** Warning logs indicate `provider_failure: false`, `code: "service_busy"`, `reason: "10 requests in flight (limit 10)"`.
- **Traces:** In Google Cloud Trace, requests with `503 service_busy` have a short duration (~10 ms) and **no outbound Dropbox span**, proving they were shed at the boundary.
- **Headers:** Responses return `Retry-After: 1`.

### Triage Steps
1. Check caller IP distribution in Cloud Logging to identify whether this is an abnormal burst, an abusive client loop, or genuine concurrent traffic:
   ```bash
   gcloud logging read 'resource.labels.service_name="cloud-storage-service-demo"
     jsonPayload.code="service_busy"' --freshness=15m --limit=20 \
     --format="table(timestamp,httpRequest.remoteIp,jsonPayload.reason)"
   ```
2. Check Dropbox App Console for account-level daily API call limits and storage quotas.

### Safe Mitigation
1. **Client Burst / Normal Overload:** The concurrency middleware rejects excess requests safely to protect worker threads and Dropbox API quotas. The service recovers automatically as soon as requests finish. Retrying after `Retry-After: 1` is completely safe.
2. **Sustained Traffic Exceeding Capacity:**
   - If instances are saturating because of low concurrency limits, update `MAX_IN_FLIGHT_REQUESTS` in `infra/run.tf` or via environment variable update (default is 10, max Cloud Run container concurrency is 20):
     ```bash
     gcloud run services update cloud-storage-service-demo --region=us-central1 --update-env-vars=MAX_IN_FLIGHT_REQUESTS=15
     ```
   - If Dropbox storage quota is exhausted, clean up stale test artifacts or request an account quota increase.

### What Recovery Cannot Undo
Rejected requests with `503 service_busy` were rejected before executing; no Dropbox state was changed.

### Recovery Verification
1. Confirm active in-flight count is below limit and new requests receive `200 OK`:
   ```bash
   curl -s -i "https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app/files/metadata?path=/nonexistent"
   ```
   (Should return `404 not_found`, confirming request was processed and not shed).
2. Run smoke check:
   ```bash
   uv run --no-project --with httpx python scripts/consumer_workflow.py https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app
   ```

---

## Failure Mode 4: Bad Release (`500 internal_error` or Regressions)

### Symptom and Telemetry
- **Alert:** 5xx alert fires immediately after a new deployment or traffic shift.
- **Logs:** Logs show `500 internal_error`, unhandled Python exceptions, or contract mismatches (e.g. invalid response format, missing required fields).
- **Traces:** Root span shows `500` status originating from application code rather than Dropbox.
- **Pipeline:** Deployment smoke check fails during release, or consumers report breaking contract changes.

### Triage Steps
1. Identify the active deployed revision and compare with the latest release commit:
   ```bash
   gcloud run services describe cloud-storage-service-demo --region=us-central1 \
     --format="table(status.traffic[].revisionName,status.traffic[].percent,status.traffic[].tag)"
   ```
2. Check logs for exception stack traces associated with the new revision:
   ```bash
   gcloud logging read 'resource.labels.service_name="cloud-storage-service-demo"
     httpRequest.status=500' --freshness=15m --limit=10
   ```

### Safe Mitigation (Rollback First, Investigate After)
1. **Roll back traffic immediately** to the previous known-good revision tag using `gcloud` (traffic switch completes within ~4 seconds):
   ```bash
   # List available revisions to find previous tag
   gcloud run revisions list --service=cloud-storage-service-demo --region=us-central1
   # Route 100% traffic to previous known-good release tag
   gcloud run services update-traffic cloud-storage-service-demo --region=us-central1 \
     --to-tags=rel-<previous_short_sha>=100
   ```
2. Re-run the consumer workflow against the service to confirm rollback took effect:
   ```bash
   uv run --no-project --with httpx python scripts/consumer_workflow.py https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app
   ```

### What Recovery Cannot Undo
Rollback restores code and configuration instantly. **Rollback does not undo changes made to Dropbox data** (e.g. files created, modified, corrupted, or deleted during the faulty release).
To repair Dropbox data:
1. Search Cloud Logging for write requests served by the bad revision (`POST /files`, `PUT /files`, `PATCH /files`, `DELETE /files`).
2. Compare modified paths against Dropbox's file version history via the Dropbox Web Console.
3. Dropbox maintains a 30-day version history and trash bin; restore deleted or corrupted files manually from version history.

### Recovery Verification
1. Service URL serves traffic from the known-good revision:
   ```bash
   gcloud run services describe cloud-storage-service-demo --region=us-central1 \
     --format="table(status.traffic[].revisionName,status.traffic[].percent)"
   ```
2. All 9 steps of the consumer workflow pass:
   ```bash
   uv run --no-project --with httpx python scripts/consumer_workflow.py https://cloud-storage-service-demo-gyesjhieva-uc.a.run.app
   ```
3. Cloud Monitoring alert resolves and emails "closed".

---

## Post-Incident Actions
1. Follow the [Error Budget Policy](error-budget-policy.md) to check remaining budget via `scripts/slo_report.py`.
2. If the error budget is spent, enforce a feature freeze until stability is restored.
3. Conduct a blameless postmortem within 48 hours to document the timeline, root cause, and preventative action items.

---

## Alert Test Record

| Date | Environment | How it was triggered | Opened (email received) | Closed | Notes |
|---|---|---|---|---|---|
| 2026-11-11 | `test` | 8 bursts of 60 concurrent `GET /files/metadata` (20:33:54–20:35:27 UTC) with `BASE_URL=<test url> uv run python scripts/overload_demo.py /alert-drill/does-not-exist.txt 60 60`, run 4 times 20 s apart. 301 requests got `503 service_busy`; Cloud Monitoring counted 221 + 80 5xx in the minutes ending 20:35 and 20:36, then 0. | Yes: "opened" email received at the alert address | Yes: "closed" email received after the errors stopped | Read-only requests for a missing file, so no Dropbox data changed. The other 189 requests were `404` (caller errors, not counted). No action needed: the limiter recovers when the burst ends. |
