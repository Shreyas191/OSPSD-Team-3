# Rollback Drill

This page covers HW2 Milestone 3, Level 2, item 2.7: deploy a deliberately broken revision, check that the pipeline stops it, roll back, check that the smoke check passes again, and say what a rollback restores and what it can't. The pipeline itself is described in [Release Pipeline](release-pipeline.md).

**Environment:** `cloud-storage-service-test` only. It is disposable; the demo service was never touched.
**Owner:** John.
**Status:** run on Nov 11, 2026, 20:53–20:56 UTC. Every check passed (evidence 2). The pipeline's gate had also stopped two real faulty releases earlier that day (evidence 1).

## Result

| Check | Expected | Observed |
|---|---|---|
| The faulty release is deployed by the pipeline's own deploy step | a new revision with no traffic | `00010-yep` (`rel-602cf95`), 0% |
| Health checks on the faulty revision | can't see the fault | `/health` 200, `/health/provider` 200 |
| Smoke check on the faulty revision | fails | failed at step 9: "deleted file is 404", expected 404, observed 200 |
| The deploy step stops | exits non-zero, no traffic moves | exit code 1; `00008-ned` (`rel-885a232`) kept 100% |
| Faulty release reaching users (simulated) | smoke check fails on the service URL | failed at step 9; requests served by `00010-yep` |
| Rollback | one command, traffic back on the good revision | `update-traffic --to-tags=rel-885a232=100`, 4 s |
| Smoke check after rollback | passes | all 9 steps passed; requests served by `00008-ned` |
| Dropbox after rollback | the faulty revision's files are still there | 2 leftover folders, each holding `archive/q3-final.txt` (17,000 bytes); deleted by hand |
| Faulty revision's URL | stops answering once its tag is removed | `rel-602cf95---…/health` → 404 |
| Server errors during the drill | none, so the 5xx alert (item 3.5) stays quiet | 0 responses with status ≥ 500 |

## What the drill checks

1. **The gate.** A faulty release is deployed with no traffic, fails the smoke check, and never receives traffic. The previous revision keeps serving.
2. **The rollback.** If a faulty release does reach users, one command sends traffic back to the previous good revision, and the same smoke check passes again.
3. **The limits.** What that command restores, and what stays broken or changed after it.

## The fault

`DELETE /files` answers `204 No Content` but deletes nothing:

```diff
 def delete_entry(path: FolderPath, client: StorageClient) -> None:
     ...
-    client.delete(path)
+    # DELIBERATE FAULT for the rollback drill (item 2.7, docs/rollback-drill.md):
+    # answer 204 without deleting anything. Never merge or push this commit.
+    del path, client
```

It sits in commit `602cf95`, made on top of `hw2` at `885a232` (the release serving `test` at the time) and never pushed to GitHub. So the faulty revision differs from the good one by these three lines only. The image was built from it with `scripts/build_image.sh`, like every release: tag and `org.opencontainers.image.revision` label `602cf950302b91bc25f1da570030a380f7b83b72`, digest `sha256:0e9ed3def0ed25d128bcc14b401bad1a2eec89492929c3ebd92a9dc56e58b77b`.

Why this fault:

- **Health checks can't see it.** `GET /health` never calls Dropbox, and `GET /health/provider` only checks the credentials. Both return `200`. Only the consumer workflow (`scripts/consumer_workflow.py`) notices, at its last step: the deleted file still has metadata.
- **It produces no server errors**, so the 5xx alert can't see it either. Only the smoke check stands between it and users.
- **It changes Dropbox.** The workflow's cleanup also calls `DELETE`, so each run against the faulty revision leaves its scratch folder behind. That shows, with real files, what a rollback can't undo.

The drill skips the CI checks on purpose. Unit tests would catch this particular fault, but the gate exists for faults CI can't see: a change in Dropbox's behaviour, a wrong setting in one environment, an image built differently. Injecting the fault after CI tests that last gate on its own.

Before the run, the fault was checked against the in-memory server (`scripts/serve_in_memory.py`): the workflow passed its first 8 steps and failed at step 9, and its cleanup left the folder behind.

## Evidence 1: the gate stopped two real faulty releases

On Nov 11, 2026 the pipeline's first two releases to `test` failed their smoke check. The bug was in the smoke script, which tried to parse the empty body of a `204` as JSON (fixed in #49). Each time the job failed before moving traffic, and the revision that was already serving kept 100%. From the Cloud Run audit log (`ReplaceService` calls by `storage-deployer`) and the CircleCI job logs, all times UTC:

| Time | CircleCI `deploy_test` job | New revision (tag) | Smoke check | Serving 100% afterwards |
|---|---|---|---|---|
| 05:51 | [825](https://app.circleci.com/workflow/241e2807-328b-4608-b18e-0a0388223055/job/9a276ad8-c234-4480-83ae-5759af49b5fa) (PR #47, `4cbbf31`) | `00002-cut` (`rel-4cbbf31`), no traffic | failed, job failed | `00001-27j`, unchanged |
| 05:59 | [834](https://app.circleci.com/workflow/3f486dec-0542-4f90-a158-adc033c246fe/job/8ba4a458-6261-45b6-9538-c59b8116543d) (PR #48, `1930295`) | `00003-doj` (`rel-1930295`), no traffic | failed, job failed | `00001-27j`, unchanged |
| 06:00–06:01 | [845](https://app.circleci.com/workflow/1297737a-f647-4364-ae67-93ed20ee937d/job/8828e646-21de-4be8-9ea8-810b5e98ca79) (PR #49, `524d715`) | `00004-zam` (`rel-524d715`), no traffic | passed | `00004-zam` |
| 06:22 | [862](https://app.circleci.com/workflow/f9a9fc0f-9cab-47b0-9bfc-ec6bacad924a/job/ccced404-6f5c-4ad9-9960-99eea9b766bd) (PR #50, `7e73aa8`) | `00006-yud` (`rel-7e73aa8`), no traffic | passed | `00006-yud` |

The failed jobs ended with the smoke script's traceback and `Exited with code exit status 1`, before `scripts/deploy.sh` reached `update-traffic`. `00001-27j` is the placeholder revision OpenTofu creates, so these runs show that traffic stays put. They don't show a rollback, and their fault was in the check, not the service. The deliberate drill below covers both.

## Evidence 2: the deliberate drill, Nov 11, 2026

Run by John from a clean `hw2` checkout at `885a232`, with `scripts/deploy.sh` and `scripts/consumer_workflow.py` unchanged. No merges into `hw2` happened during the run. Times are UTC.

**Before (20:53:44).** `00008-ned` (`rel-885a232`) served 100%. `/health` and `/health/provider` returned 200. The test app's Dropbox had no `/workflow-…` folders.

**Part A, the gate (20:55:06–20:55:33).** The pipeline's deploy step, given the faulty image:

```text
== Deploying …/cloud-storage-service@sha256:0e9ed3de… to cloud-storage-service-test as rel-602cf95, with no traffic
Service [cloud-storage-service-test] revision [cloud-storage-service-test-00010-yep] has been deployed and is serving 0 percent of traffic.
== Smoke-checking https://rel-602cf95---cloud-storage-service-test-gyesjhieva-uc.a.run.app
GET /health -> 200
GET /health/provider -> 200
PASS upload … PASS metadata … PASS download … PASS list … PASS move …
PASS metadata at new path … PASS old path is gone …
PASS delete: DELETE /files {'path': '/workflow-c98dbb4c4b9b/archive/q3-final.txt'}
     expected 204, observed 204 (0 bytes)
FAIL deleted file is 404: GET /files/metadata {'path': '/workflow-c98dbb4c4b9b/archive/q3-final.txt'}
     expected 404, observed 200 {'id': 'id:KzOOg5ouvzUAAAAAAAABYw', 'name': 'q3-final.txt', …}
cleanup: DELETE /workflow-c98dbb4c4b9b -> 204
deploy.sh exit code: 1
```

Traffic afterwards: `00008-ned` 100%, `00010-yep` 0%. The script never reached `update-traffic`, so users never saw the faulty revision.

**Part B, a faulty release that reached users (20:55:33–20:55:45).** To rehearse the rollback, traffic was sent to the faulty revision on purpose: `gcloud run services update-traffic cloud-storage-service-test --to-tags=rel-602cf95=100`. On the service URL, `/health` and `/health/provider` still returned 200, and the smoke check failed at the same step (`/workflow-bbb2f788f8a4`, expected 404, observed 200).

**Rollback (20:55:45–20:55:59).** `gcloud run services update-traffic cloud-storage-service-test --to-tags=rel-885a232=100` took 4 seconds. Then on the service URL: `/health` 200, `/health/provider` 200, and the smoke check passed all 9 steps, including `PASS deleted file is 404 … expected 404, observed 404`.

**After the rollback (20:55:59–20:56:08).** The two folders the faulty revision "deleted" were still in Dropbox, each holding `archive/q3-final.txt` (17,000 bytes). They were deleted through the good revision (`204`, then metadata `404`). Then the faulty revision's tag was removed (`--remove-tags=rel-602cf95`), and its URL returned `404`.

**Which revision served what.** The Cloud Run request log for the service, 20:55:00–20:56:10 (45 requests, shortened):

| Time | Revision | URL | Requests |
|---|---|---|---|
| 20:55:24–20:55:31 | `00010-yep` (faulty) | tag URL `rel-602cf95---…` | Part A smoke check: 12 requests, including `DELETE` 204 then metadata **200** |
| 20:55:32 | `00008-ned` (good) | service URL | the drill's listing of `/` |
| 20:55:38–20:55:45 | `00010-yep` (faulty) | service URL | Part B smoke check: 12 requests, same failure |
| 20:55:50–20:55:58 | `00008-ned` (good) | service URL | smoke check after rollback: 12 requests, metadata after `DELETE` **404** |
| 20:55:59–20:56:04 | `00008-ned` (good) | service URL | listing and deleting the 2 leftover folders |

So the faulty revision held the service URL for about 15 seconds, from the traffic switch at 20:55:33 to the rollback finishing at about 20:55:49, and only the drill's own requests reached it. No request in the window returned a status of 500 or above.

## Findings

- **Failed releases stay reachable.** `scripts/deploy.sh` leaves a failed candidate with its `rel-…` tag, so its URL keeps answering, publicly. The drill removed `rel-602cf95` by hand, but the tags of the earlier releases (`rel-4cbbf31`, `rel-1930295`) are still there. Suggested fix: when the smoke check fails, have `deploy.sh` remove the candidate's tag before it exits.
- **The 5xx alert can't see silent data faults.** This fault returned only 2xx and 4xx. Only the smoke check, which runs real operations and checks their results, caught it. Keep the smoke check as the release gate, not the alert.
- **The faulty image stays in Artifact Registry** until the cleanup policy (keep the 5 newest) removes it, and it counts as one of those 5. Deleting it needs more than the deployer's writer role.

## Repeat the drill

**Access.** The person running it needs, on test only, the same three roles the pipeline's deployer has. The project owner granted them on Nov 11, 2026 for this run.

| Role | On |
|---|---|
| `roles/run.developer` | the service `cloud-storage-service-test` |
| `roles/iam.serviceAccountUser` | the service account `storage-run-test` (a deploy runs the revision as it) |
| `roles/artifactregistry.writer` | the repository `cloud-storage-service` (to push the faulty image) |

The project owner grants them with `gcloud run services add-iam-policy-binding`, `gcloud iam service-accounts add-iam-policy-binding` and `gcloud artifacts repositories add-iam-policy-binding`, and can remove them after the drill. These are non-authoritative bindings, so OpenTofu's own bindings (`google_*_iam_member` in `infra/iam.tf`) are unaffected.

**Before you start.** Tell the team not to merge into `hw2` for 20 minutes: a merge deploys to `test` and moves its traffic, which would mix with the drill.

**Steps.** `REGION=us-central1`, `SERVICE=cloud-storage-service-test`, and `GOOD` is the tag of the revision serving before the drill (`gcloud run services describe $SERVICE --region=$REGION`).

1. Commit the fault on top of the serving release, build it with `scripts/build_image.sh <repository>`, push it, and note its digest.
2. **Gate.** Run the pipeline's deploy step unchanged, from a clean `hw2` checkout:

   ```bash
   GCP_PROJECT_ID=ospsd-team3 scripts/deploy.sh test <repository>@sha256:<digest> <faulty sha>
   ```

   Expected: `/health` and `/health/provider` pass, the workflow fails at "deleted file is 404", the script exits non-zero, and `GOOD` still has 100%.
3. **Faulty release reaches users.** Simulate a fault that got past the gate:

   ```bash
   gcloud run services update-traffic $SERVICE --region=$REGION --to-tags=rel-<faulty short sha>=100
   uv run --no-project --with httpx python scripts/consumer_workflow.py <service url>   # expect FAIL
   ```

4. **Roll back** and check again:

   ```bash
   gcloud run services update-traffic $SERVICE --region=$REGION --to-tags=$GOOD=100
   uv run --no-project --with httpx python scripts/consumer_workflow.py <service url>   # expect PASS
   ```

5. **Clean up Dropbox.** List `GET /files?path=/` and delete each leftover `/workflow-…` folder through the good revision.
6. **Remove the faulty revision's tag**, so its URL stops answering: `gcloud run services update-traffic $SERVICE --region=$REGION --remove-tags=rel-<faulty short sha>`.
7. **Record** the run below, with the revisions, the times and the Cloud Run request log (each request carries the revision that served it).

## What a rollback restores and what it can't

`update-traffic` only chooses which revision answers. A revision is fixed when it is created, so whatever is part of it comes back. Anything that lives outside it does not.

| Restored | Not restored |
|---|---|
| The image: our code and its locked dependencies, by digest, including the `RELEASE` the image sets | **Files in Dropbox.** Whatever the faulty revision uploaded, moved, overwrote or deleted stays that way (the drill's 2 leftover folders). Find its requests in the Cloud Run request log, filtered by revision; Dropbox's file history can restore deleted or overwritten files for a while. |
| Environment variables and labels set on the revision | **Secret values.** Both revisions read the secrets as `latest`, so a bad credential added to Secret Manager breaks the revision you roll back to as well. Fix it in Secret Manager by adding a new version with the good value; instances read it when they start ([Runbook](runbook.md)). |
| Which secrets the revision reads, and under which variable names | **Settings of the service itself:** who can call it (IAM), ingress, the traffic tags. |
| CPU, memory, concurrency, probes, instance limits and runtime identity set in the revision template | **Infrastructure** changed with OpenTofu. Revert `infra/` and apply it in a reviewed pull request. |
| | **Answers already given.** Clients that called the faulty revision were told, for example, that a file was deleted when it wasn't. |
| | **The faulty revision itself.** It still exists, and while it has a tag its tag URL keeps answering, publicly. Remove the tag (step 6). |
| | **The next release.** A rollback only moves traffic. The next merge into `hw2` (or `main`) deploys the newest commit and moves traffic to it if the smoke check passes, so revert the faulty commit before merging anything else. A repeated `tofu apply` doesn't undo a rollback, because `infra/run.tf` ignores the image. |

## Record of runs

| Date | Run by | Faulty revision | Rolled back to | Result |
|---|---|---|---|---|
| Nov 11, 2026, 20:53–20:56 UTC | John | `00010-yep` (`rel-602cf95`, DELETE does nothing) | `00008-ned` (`rel-885a232`) | Gate stopped it (exit 1, no traffic). After a simulated escape, rollback took 4 s and the smoke check passed again. 2 leftover Dropbox folders deleted by hand; tag removed. |
