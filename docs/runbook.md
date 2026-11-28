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

Most often the refresh token was revoked or the Dropbox app's secret changed. Log in again to that environment's Dropbox app, add the new token as a new secret version, then start a new revision from the image that is serving now and send it the traffic, so the token is read (secrets are read when an instance starts). A new revision gets no traffic until it's moved: each release pins traffic to its own tagged revision. It must also run the serving image, not the template's, which may hold a release that failed its smoke check ([Rollout](rollout.md), stop action 1).

```bash
uv run python -m dropbox_client_impl login
uv run python -c 'import json; print(json.load(open(".dropbox_token.json"))["refresh_token"], end="")' \
  | gcloud secrets versions add dropbox-refresh-token-demo --data-file=-
SERVICE=cloud-storage-service-demo REGION=us-central1
serving=$(gcloud run services describe "$SERVICE" --region="$REGION" --format=json \
  | python3 -c 'import json,sys; print(next(t["revisionName"] for t in json.load(sys.stdin)["status"]["traffic"] if t.get("percent") == 100))')
image=$(gcloud run revisions describe "$serving" --region="$REGION" --format='value(spec.containers[0].image)')
gcloud run services update "$SERVICE" --region="$REGION" \
  --image="$image" --update-labels=secret-rotated=$(date +%s) --tag=rotated
gcloud run services update-traffic "$SERVICE" --region="$REGION" --to-tags=rotated=100
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

## Alert: students' turns are failing

**What it means:** on one agent service (`course-agent-test` or `-demo`), either more than 3 students' turns ended in 5 minutes because the model (Gemini) or the API service was unavailable, or the agent service itself returned more than 5 server errors. Students are getting "the assistant is busy" or "I couldn't reach your course data" instead of an answer. Nothing was changed in their accounts: a turn only reads and proposes. The alert is defined in `infra/agent_monitoring.tf`; it exists because a failed turn still answers `200`, which the 5xx alert above can't see. Which failures lead here and what each leaves behind: [Failure Analysis](failure-analysis.md).

### 1. Find which failure

Each turn writes one run record (`"event": "agent_run"`) with its stop reason and, per tool call, the tool's name and outcome:

```bash
gcloud logging read 'jsonPayload.event="agent_run"
  jsonPayload.stop_reason=("model_unavailable" OR "api_unavailable")' \
  --freshness=30m --limit=20 \
  --format="table(timestamp,resource.labels.service_name,jsonPayload.stop_reason,jsonPayload.tools)"
```

| What you see | Likely cause | Do this |
|---|---|---|
| `model_unavailable`, students told "busy" | Gemini's per-minute or per-day quota, or Gemini overloaded | Check usage and limits in AI Studio. A per-minute limit clears within a minute; a per-day limit resets at midnight Pacific time. If it's the daily quota during launch week, see the paid-tier recommendation in the [readiness record](https://github.com/Shreyas191/OSPSD-Team-3/pull/117) |
| `model_unavailable`, students told "failed", on every turn | Gemini rejects our key (revoked, or the project's billing changed) | Add a new key from a hidden prompt (the command in `infra/agent.tf`), then start a new revision from the serving image and move traffic to it, as for the Dropbox token above (with `SERVICE=course-agent-<env>`); without the traffic move, the new key is never used |
| `api_unavailable`, and the API's 5xx alert is open too | The API service or the database is down | Follow [the 5xx alert](#alert-callers-are-getting-server-errors-5xx). Check `GET /health/database` (Neon) and `GET /health/provider` (Dropbox) on the API service |
| `api_unavailable`, only on `read_document` or `list_course_documents` in `tools` | Dropbox unreachable or refusing our token | [Dropbox unreachable](#dropbox-unreachable-503-provider_unavailable) or [credentials](#dropbox-rejects-our-credentials-502-provider_error) |
| Agent 5xx, starting with a new revision | A bad release of the agent | Roll the agent service back (see "Roll back" in [Release Pipeline](release-pipeline.md)) |

### 2. Confirm it's fixed

- the alert email says **closed** (once 5 minutes pass with 3 or fewer failed turns and 5 or fewer agent 5xx);
- the agent dashboard's Task success panel shows turns ending `replied` or `proposal_ready` again.

**Recovery can't undo:** nothing to undo. A failed turn changed nothing; the student sends the message again. If the agent crashed after `propose_changes` stored a proposal, that proposal is still pending and the student can review it.

## A proposal stuck in `executing`

**Seen in:** a student reports that running their changes again fails with `409` ("it can't run now"), and the proposal's status is `executing` long after the request ended.

The API instance stopped while it was executing (a redeploy, a crash or a timeout), between a provider write and saving that change's outcome ([Failure Analysis](failure-analysis.md), item 1). Nothing moves it on by itself yet. With the student's agreement: for each change that has no outcome, check their calendar or board for it; tell them what you found; then set the proposal's status to `failed` so the changes without an outcome can run again. Only do that once you're sure the unrecorded change wasn't made, or after the student has removed it, otherwise running again makes it twice.

To find stuck proposals (read-only):

```sql
SELECT id, created_at, decided_at FROM proposals WHERE status = 'executing';
```

## Alert test record

| Date | Environment | How it was triggered | Opened (email received) | Closed | Notes |
|---|---|---|---|---|---|
| 2026-11-11 | `test` | 8 bursts of 60 concurrent `GET /files/metadata` (20:33:54–20:35:27 UTC) with `BASE_URL=<test url> uv run python scripts/overload_demo.py /alert-drill/does-not-exist.txt 60 60`, run 4 times 20 s apart. 301 requests got `503 service_busy`; Cloud Monitoring counted 221 + 80 5xx in the minutes ending 20:35 and 20:36, then 0. | Yes: "opened" email received at the alert address | Yes: "closed" email received after the errors stopped | Read-only requests for a missing file, so no Dropbox data changed. The other 189 requests were `404` (caller errors, not counted). No action needed: the limiter recovers when the burst ends. |
| Pending | `test` | **Agent alert**: the drill below, once the agent loop (#92) is deployed and `agent_monitoring.tf` is applied | | | Expected: opens about 6 minutes after the first failed turn (5-minute window, held for 1 minute); closes about 5 minutes after the last. |

### Agent alert drill

Makes 5 turns fail with `model_unavailable` on a test revision that serves no traffic, then puts the service back. It changes no student data: a failed turn only reads.

```bash
# 1. A new revision that serves no traffic, with an invalid model key. This also changes the
#    service's template, so step 3 must put the real key back before anything else deploys.
gcloud run services update course-agent-test --region=us-central1 --no-traffic --tag=alertdrill \
  --remove-secrets=GEMINI_API_KEY --update-env-vars=GEMINI_API_KEY=invalid-for-drill
# 2. Send 5 turns to https://alertdrill---course-agent-test-<hash>-uc.a.run.app within 2 minutes, signed in as a test user.
# 3. Put the real key back on the service template, then drop the tag.
gcloud run services update course-agent-test --region=us-central1 --no-traffic \
  --remove-env-vars=GEMINI_API_KEY --update-secrets=GEMINI_API_KEY=gemini-api-key-test:latest
gcloud run services update-traffic course-agent-test --region=us-central1 --remove-tags=alertdrill
```

Record when the "opened" and "closed" emails arrived in the table above.
