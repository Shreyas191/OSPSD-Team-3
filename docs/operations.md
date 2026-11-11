# Operating the Service

Start here if you have to look after the deployed service and didn't build it. This page answers five questions, each with the command to run and what a normal answer looks like: what is running, which version, is it healthy, where are its logs, and how to roll it back. It links to the detailed pages only for what goes beyond these questions.

**You need:** `gcloud` signed in to project `ospsd-team3` with at least the Viewer role (ask the project owner, Shreyas Kaldate, for access), and `curl`. Every command below is for the **test** service. For demo, replace `test` with `demo` in the service name and URL.

```bash
gcloud config set project ospsd-team3
SVC=cloud-storage-service-test
URL=https://cloud-storage-service-test-gyesjhieva-uc.a.run.app
```

## 1. What is running?

One HTTP service (FastAPI, in this repository) stores files in Dropbox. It runs on **Google Cloud Run** in region `us-central1`, in two copies:

| Service | Deployed from | Dropbox app | Status on Nov 11, 2026 |
|---|---|---|---|
| `cloud-storage-service-test` | every push to `hw2` | test app | Serving our service |
| `cloud-storage-service-demo` | every push to `hw2`, after test passed its smoke check | demo app | Google's placeholder container ("It's running!") until the first `hw2` release that includes `deploy_demo` |

Around them, all created by OpenTofu from `infra/` ([Infrastructure](infrastructure.md)):

- **Container images** in Artifact Registry, `us-central1-docker.pkg.dev/ospsd-team3/cloud-storage-service/cloud-storage-service`, one per commit, tagged with the commit SHA.
- **Dropbox credentials** in Secret Manager, three per environment. Cloud Run passes them to the service at startup. They are never in the image or the repository.
- **Telemetry** in Cloud Logging, Cloud Trace and Cloud Monitoring: a dashboard and one alert ([Dashboard](dashboard.md), [Runbook](runbook.md)).
- **The pipeline** in CircleCI (`.circleci/config.yml`), which builds, deploys and checks each release ([Release Pipeline](release-pipeline.md)).

```bash
gcloud run services describe $SVC --region=us-central1 --format="value(status.url,status.conditions[0].status)"
```

A normal answer is the service URL followed by `True` (the service is ready).

## 2. Which version is running?

Each release is a Cloud Run revision tagged `rel-<first 7 characters of the commit SHA>`. One revision gets all the traffic. Older ones stay for rollback with 0%:

```bash
gcloud run services describe $SVC --region=us-central1 --flatten="status.traffic[]" \
  --format="table(status.traffic.percent,status.traffic.revisionName,status.traffic.tag)" \
  | awk 'NR==1 || $1 ~ /^[0-9]+$/'
```

```text
PERCENT  REVISION_NAME                         TAG
100      cloud-storage-service-test-00030-xup  rel-7d6b6ff
```

Don't read only the first traffic entry (`status.traffic[0]`): it is often an old, tagged revision with no traffic. Open the commit at `https://github.com/Shreyas191/OSPSD-Team-3/commit/7d6b6ff`. The CircleCI pipeline for that commit shows the checks it passed.

Two other places show the same thing:

- every request's log line carries `release`, the full commit SHA (question 4);
- the dashboard's **Release serving requests** panel shows which commit answered requests over time.

## 3. Is it healthy?

```bash
curl -s $URL/health            # the process is up; never calls Dropbox
curl -s $URL/health/provider   # Dropbox accepts this environment's credentials
```

Both answer `{"status":"ok"}` with status 200 when healthy. If `/health` works but `/health/provider` returns `503 provider_unavailable` or `502 provider_error`, the service is up and Dropbox is the problem: see the [Runbook](runbook.md).

To check every shared operation end to end, run the vertical's consumer workflow. It is the same check the pipeline runs before each release, and it cleans up after itself:

```bash
uv run --no-project --with httpx python scripts/consumer_workflow.py $URL
```

For trends, open the dashboard: Cloud Monitoring → Dashboards → **Cloud storage service: golden signals**. It shows traffic, errors, latency and saturation for each operation. If no one has called the service recently, the charts say "No data". That means no traffic, not no errors. The **5xx alert** emails the team when one service returns more than 5 server errors within 5 minutes. The [service level objectives](slo.md) say what "healthy enough" means, and `scripts/slo_report.py` measures them.

## 4. Where are its logs?

Cloud Logging, under the Cloud Run revision. The service writes **one JSON line per request**: operation, status, outcome (`ok` or the error code), duration, release and trace ID. File paths are never logged.

```bash
gcloud logging read "resource.type=\"cloud_run_revision\" resource.labels.service_name=\"$SVC\"
  jsonPayload.event=\"request\" jsonPayload.operation!=\"check_service_health\"" \
  --freshness=1h --limit=20 \
  --format="table(timestamp,jsonPayload.operation,jsonPayload.status,jsonPayload.outcome,jsonPayload.duration_ms,jsonPayload.release)"
```

```text
TIMESTAMP                    OPERATION              STATUS  OUTCOME    DURATION_MS  RELEASE
2026-11-11T22:39:27.114474Z  list_folder            404     not_found  116.9        7d6b6ff356e3fba5189392643d409c8b257563d6
2026-11-11T22:39:26.854173Z  delete_entry           204     ok         634.6        7d6b6ff356e3fba5189392643d409c8b257563d6
2026-11-11T22:39:26.092522Z  check_provider_health  200     ok         276.7        7d6b6ff356e3fba5189392643d409c8b257563d6
```

The filter leaves out the liveness probe's `/health` calls, which arrive every 30 seconds. Add `jsonPayload.status>=500` to see only server errors. In the console: Logging → Logs Explorer, the same filter, or the dashboard's **Failed requests** panel. Each entry links to its trace in Cloud Trace, which shows the time spent in each Dropbox call. Cloud Run also logs each request itself, under `run.googleapis.com/requests`. Those entries include the full URL and its query string, which can contain file paths. Logs are kept for 30 days.

If `gcloud logging read` fails with a quota-project error on a personal account, prefix it with `CLOUDSDK_BILLING_QUOTA_PROJECT=LEGACY`. [Telemetry Walkthrough](telemetry-walkthrough.md) follows one request from log to trace to metric.

## 5. How do I roll it back?

Send all traffic back to the previous good revision's tag. Nothing is rebuilt:

```bash
gcloud run revisions list --service=$SVC --region=us-central1 \
  --format="table(name,metadata.labels.release,metadata.creationTimestamp)" --limit=5
gcloud run services update-traffic $SVC --region=us-central1 --to-tags=rel-<previous short sha>=100
```

Then confirm with question 2, and check that it works with the consumer workflow in question 3. Changing traffic needs the Cloud Run Developer role, not just Viewer.

- **What a rollback doesn't undo:** files that a bad release created, changed or deleted in Dropbox stay that way.
- **The next push to the branch deploys again** and moves traffic to the new commit. If the bad change is still in the branch, revert it there too.
- **Practised end to end:** see the [Rollback Drill](rollback-drill.md).

## Teardown, cost and owner

- **Owner:** Shreyas Kaldate, who owns the Google Cloud project and its billing.
- **Teardown:** Dec 18, 2026, following [Teardown](teardown.md).
- **Cost:** see "Cost, ownership and teardown" in [`DESIGN.md`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/DESIGN.md#cost-ownership-and-teardown). In short, it is $0 so far, all within free tiers, and a $5 budget alert emails the owner.

## Stranger test record

Rubric 17.2: a teammate who didn't build the infrastructure answers the five questions using only the docs, in about ten minutes, without reading source code or CI configuration. Record each run here, including the time taken and any question that took longer than expected or needed help. Then fix the docs.

| Date | Teammate | Service | Time | Answered unaided | What was hard, and the fix |
|---|---|---|---|---|---|
| — | Not run yet | — | — | — | — |

A finding from writing this page, before any test: the "Which version is running?" command in [Release Pipeline](release-pipeline.md) read `status.traffic[0]`. On Nov 11 that entry was an old revision with 0% traffic (`rel-4cbbf31`), while `rel-7d6b6ff` was serving. Both pages now use the command in question 2.
