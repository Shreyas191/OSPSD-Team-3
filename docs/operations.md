# Operating the Service

Start here if you have to look after the deployed service and didn't build it. This page answers five questions, each with the command to run and what a normal answer looks like: what is running, which version, is it healthy, where are its logs, and how to roll it back. It links to the detailed pages only for what goes beyond these questions.

**You need:** `gcloud` signed in to project `ospsd-team3` with at least the Viewer role (ask the project owner, Shreyas Kaldate, for access), and `curl`.

- **No `gcloud` on your machine?** Either install it ([Google Cloud CLI install guide](https://cloud.google.com/sdk/docs/install); on macOS, `brew install --cask gcloud-cli`) and run `gcloud auth login`, or use **Cloud Shell**: open the [Google Cloud console](https://console.cloud.google.com/) for project `ospsd-team3` and click **Activate Cloud Shell**. Cloud Shell already has `gcloud`, signed in as you.
- **Without `gcloud`** you can answer only questions 1 and 3. Questions 2, 4 and 5 need it. Every command below is for the **test** service. For demo, replace `test` with `demo` in the service name and URL.

**Run the commands; don't copy the examples.** Each answer is what a command prints when you run it. The outputs on this page are examples from Nov 11, 2026. The release, revision names and log lines will be different when you look, because every push to `hw2` deploys a new release.

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
| `cloud-storage-service-demo` | every push to `hw2`, after test passed its smoke check | demo app | Serving our service since its first deploy, late on Nov 11 (before that, Google's placeholder container) |

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

Each release is a Cloud Run revision tagged `rel-<first 7 characters of the commit SHA>`. One revision gets all the traffic. Older ones stay for rollback with 0%. **Run:**

```bash
gcloud run services describe $SVC --region=us-central1 --flatten="status.traffic[]" \
  --format="table(status.traffic.percent,status.traffic.revisionName,status.traffic.tag)" \
  | awk 'NR==1 || $1 ~ /^[0-9]+$/'
```

*Example only (Nov 11, 2026). Your output shows whichever release is serving now:*

```text
PERCENT  REVISION_NAME                         TAG
100      cloud-storage-service-test-00030-xup  rel-7d6b6ff
```

If the command prints only the `PERCENT` header and no row (this happened once in Cloud Shell), show the traffic as YAML instead and find the entry with `percent: 100`:

```bash
gcloud run services describe $SVC --region=us-central1 --format="yaml(status.traffic)"
```

The answer is the row's tag: here, commit `7d6b6ff`. Don't read only the first traffic entry (`status.traffic[0]`): it is often an old, tagged revision with no traffic. Open the commit at `https://github.com/Shreyas191/OSPSD-Team-3/commit/<short sha>`. The CircleCI pipeline for that commit shows the checks it passed.

Two other places show the same thing:

- every request's log line carries `release`, the full commit SHA (question 4);
- the dashboard's **Release serving requests** panel shows which commit answered requests over time.

## 3. Is it healthy?

**Run:**

```bash
curl -s $URL/health            # the process is up; never calls Dropbox
curl -s $URL/health/provider   # Dropbox accepts this environment's credentials
```

Both answer `{"status":"ok"}` with status 200 when healthy. The answer to "is it healthy?" is what they return when you run them, not what they should return. If `/health` works but `/health/provider` returns `503 provider_unavailable` or `502 provider_error`, the service is up and Dropbox is the problem: see the [Runbook](runbook.md).

To check every shared operation end to end, run the vertical's consumer workflow. It is the same check the pipeline runs before each release, and it cleans up after itself:

```bash
uv run --no-project --with httpx python scripts/consumer_workflow.py $URL
```

For trends, open the dashboard: Cloud Monitoring → Dashboards → **Cloud storage service: golden signals**. It shows traffic, errors, latency and saturation for each operation. If no one has called the service recently, the charts say "No data". That means no traffic, not no errors. The **5xx alert** emails the team when one service returns more than 5 server errors within 5 minutes. The [service level objectives](slo.md) say what "healthy enough" means, and `scripts/slo_report.py` measures them.

## 4. Where are its logs?

Cloud Logging, under the Cloud Run revision. The service writes **one JSON line per request**: operation, status, outcome (`ok` or the error code), duration, release and trace ID. File paths are never logged. **Run:**

```bash
gcloud logging read "resource.type=\"cloud_run_revision\" resource.labels.service_name=\"$SVC\"
  jsonPayload.event=\"request\" jsonPayload.operation!=\"check_service_health\"" \
  --freshness=1h --limit=20 \
  --format="table(timestamp,jsonPayload.operation,jsonPayload.status,jsonPayload.outcome,jsonPayload.duration_ms,jsonPayload.release)"
```

*Example only (Nov 11, 2026). Your output shows the last hour's requests, or nothing if no one has called the service in that time:*

```text
TIMESTAMP                    OPERATION              STATUS  OUTCOME    DURATION_MS  RELEASE
2026-11-11T22:39:27.114474Z  list_folder            404     not_found  116.9        7d6b6ff356e3fba5189392643d409c8b257563d6
2026-11-11T22:39:26.854173Z  delete_entry           204     ok         634.6        7d6b6ff356e3fba5189392643d409c8b257563d6
2026-11-11T22:39:26.092522Z  check_provider_health  200     ok         276.7        7d6b6ff356e3fba5189392643d409c8b257563d6
```

The filter leaves out the liveness probe's `/health` calls, which arrive every 30 seconds. Add `jsonPayload.status>=500` to see only server errors. In the console: Logging → Logs Explorer, the same filter, or the dashboard's **Failed requests** panel. Each entry links to its trace in Cloud Trace, which shows the time spent in each Dropbox call. Cloud Run also logs each request itself, under `run.googleapis.com/requests`. Those entries include the full URL and its query string, which can contain file paths. Logs are kept for 30 days.

If `gcloud logging read` fails with a quota-project error on a personal account, prefix it with `CLOUDSDK_BILLING_QUOTA_PROJECT=LEGACY`. [Telemetry Walkthrough](telemetry-walkthrough.md) follows one request from log to trace to metric.

## 5. How do I roll it back?

Send all traffic back to the previous good revision's tag. Nothing is rebuilt.

First, find the previous release. **Run:**

```bash
gcloud run revisions list --service=$SVC --region=us-central1 \
  --format="table(name,metadata.labels.release,metadata.creationTimestamp)" --limit=5
```

The newest revision is listed first. The previous release is the next row whose revision didn't fail; its `RELEASE` column is the short SHA. Then move traffic to its tag:

```bash
gcloud run services update-traffic $SVC --region=us-central1 --to-tags=rel-<that short sha>=100
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
| Nov 11, 2026 | Victor Wang (run 1) | test | Not reported | 1 of 5 fully. Questions 2–5 were answered from the page's text and example outputs, not by running the commands. | Question 2's answer was the example output (`rel-7d6b6ff`), but test was serving `rel-4929d8a`. Question 3 gave what the health checks *should* return, question 4 named Cloud Logging without showing a request, and question 5 gave the rollback command without the previous tag. **Fix:** the page now says to run the commands, not copy the examples; each example is labelled with its date; each question starts with **Run:**; and question 5 says how to find the previous tag. |
| Nov 11, 2026, about 23:56 UTC | Victor Wang (run 2) | test | Not reported | 2 of 5 fully: questions 1 and 3. Question 3 was run for real: `/health`, `/health/provider` and the consumer workflow all passed (the service's request log shows the workflow's 9 requests at 23:56:09–23:56:14 UTC on revision `00039-mew`). | `gcloud` wasn't installed, so questions 2, 4 and 5 came from the page's examples again. Question 2 gave `rel-7d6b6ff`, but `rel-4929d8a` was serving. Question 4 showed the Nov 11 example lines, and question 5 left the previous tag (`rel-96e0fde`) unresolved. He read the page as it was on `hw2`, before the fix from run 1. **Fix:** "You need" now says how to get `gcloud` (install it, or use Cloud Shell) and which questions need it. Victor's suggestion. |
| Nov 12, 2026, about 00:26 UTC | Victor Wang (run 3: questions 2, 4 and 5 again, in Cloud Shell, on the fixed page) | test | Not reported | **3 of 3, all from live output.** Checked against the service: `rel-29c7f5d` on `00043-how` was serving; the three log lines match the service's request log exactly; `rel-2596e1c` (revision `00041-rux`) is the previous release. | The question 2 table command printed only the `PERCENT` header in Cloud Shell, so Victor read the traffic from YAML instead. **Fix:** question 2 now gives the YAML command as a fallback. We couldn't reproduce the empty table with Google Cloud SDK 593.0.0 on macOS. |

**Run 1 (Victor Wang), answers as given:**

| # | Answer | Check |
|---|---|---|
| 1 | FastAPI service on Cloud Run, us-central1, project `ospsd-team3`, as `cloud-storage-service-test` | Correct |
| 2 | "The page's sample shows `rel-7d6b6ff` on revision `cloud-storage-service-test-00030-xup`" | Out of date: test was serving `rel-4929d8a` on `cloud-storage-service-test-00039-mew` |
| 3 | Both should return `{"status":"ok"}` with 200 | Expected result, not observed |
| 4 | Cloud Logging under the Cloud Run revision | Right place; no requests shown |
| 5 | `gcloud run services update-traffic cloud-storage-service-test --region=us-central1 --to-tags=rel-<previous short sha>=100` | Right command; previous tag (`rel-96e0fde`) not found |

**Run 2 (Victor Wang), answers as given, summarized:**

| # | Answer | Check |
|---|---|---|
| 1 | FastAPI document storage service on Cloud Run, `us-central1`, `ospsd-team3`. Test is serving; demo still runs the placeholder container. | Correct |
| 2 | Commit `7d6b6ff`, revision `cloud-storage-service-test-00030-xup`, taken from the page's example table | Out of date: `rel-4929d8a` on `00039-mew` was serving |
| 3 | Healthy: `/health` and `/health/provider` both `HTTP/2 200 {"status":"ok"}`; the consumer workflow passed every step and cleaned up | Correct, and observed (confirmed in the request log) |
| 4 | Cloud Logging, `resource.type="cloud_run_revision"`, with the documented query; "last few requests" copied from the example output | Right place and query; the requests shown are the Nov 11 example, not live |
| 5 | List the revisions, then `update-traffic … --to-tags=rel-<previous short sha>=100`; confirm with questions 2 and 3 | Right steps; previous tag not resolved |

Victor's other feedback: the five questions mapping one-to-one onto five headings made the page quick to navigate, and the self-contained consumer workflow command made the end-to-end check easy.

**Run 3 (Victor Wang), answers as given:**

| # | Answer | Check |
|---|---|---|
| 2 | 100% of traffic on `cloud-storage-service-test-00043-how`, tag `rel-29c7f5d` (from the YAML output; the table printed only its header) | Correct |
| 4 | `00:26:18 get_file_metadata 404 not_found 205.7`, `00:24:36 delete_entry 204 ok 841.7`, `00:24:34 get_file_metadata 404 not_found 275`, all release `29c7f5d…` | Correct: live entries, present in the request log |
| 5 | `gcloud run services update-traffic cloud-storage-service-test --region=us-central1 --to-tags=rel-2596e1c=100` (not run) | Correct: `rel-2596e1c` is the release before `rel-29c7f5d` |

**Result.** Runs 1 and 2 hit the same problem: without `gcloud`, the examples on the page looked like answers. After the fixes (examples labelled, a **Run:** step on each question, `gcloud` and Cloud Shell setup), run 3 answered every remaining question from live output. With run 2's real answers to questions 1 and 3, Victor answered all five questions from the docs without reading source code or CI configuration. The time wasn't reported for any run.

A finding from writing this page, before any test: the "Which version is running?" command in [Release Pipeline](release-pipeline.md) read `status.traffic[0]`. On Nov 11 that entry was an old revision with 0% traffic (`rel-4cbbf31`), while `rel-7d6b6ff` was serving. Both pages now use the command in question 2.
