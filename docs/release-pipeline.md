# Release Pipeline

This page covers HW2 Milestone 3, Level 2, item 2.6: how a merged change reaches the deployed service with no manual commands, how a release is checked before it serves anyone, and how to find and roll back what is running. The infrastructure it deploys to is in [Infrastructure](infrastructure.md).

## What happens on a push

| Branch | Pipeline |
|---|---|
| `hw2` (the HW2 release branch) | lint → unit tests → CI tests → conformance suite → infrastructure checks → **build image → deploy to `test` → smoke check → deploy to `demo` → smoke check** |
| `main` (not updated for HW2) | lint → unit tests → CI tests → real-Dropbox integration tests → conformance suite → infrastructure checks → **build image → deploy to `test` → smoke check → deploy to `demo` → smoke check** |
| any other branch or pull request | lint, tests, conformance suite and infrastructure checks; no Google credentials at all |

A change can only be deployed after every check before it passed: CircleCI runs the release jobs with `requires:` on the checks.

### One image per commit, deployed by digest

`release_image` runs `scripts/build_image.sh` once per commit. It builds from `uv.lock` (`uv sync --locked`), tags the image with the full commit SHA and records the SHA in the image label `org.opencontainers.image.revision`. It never tags `latest`. The deploy jobs look up that tag's **digest** and deploy `…/cloud-storage-service@sha256:…`, so what runs is exactly what was built, even if someone pushed the tag again. Configuration and secrets are not in the image; Cloud Run supplies them (see [Container and Configuration](deployment.md)).

### Deploy, check, then switch traffic

`scripts/deploy.sh <env> <image@digest> <sha>`:

1. Deploys the image as a new revision with **no traffic**, tagged `rel-<short sha>`, which gives it its own URL. Users keep reaching the previous revision.
2. Smoke-checks that URL against the real provider:
   - `GET /health` returns `200`;
   - `GET /health/provider` returns `200`, so Dropbox accepts the environment's credentials;
   - `scripts/consumer_workflow.py` runs the vertical's shared workflow unchanged: upload, metadata, download and compare bytes, list, move, metadata, delete, 404. It works inside a new `/workflow-<random>` folder and deletes it at the end, even if a step fails.
3. Only if all three passed, sends 100% of traffic to the new revision.

If any check fails, the script exits before step 3, the CircleCI job fails, and the release is **not** recorded as successful: the previous revision is still serving, untouched. A failure in `test` also stops `demo` from being deployed, so demo only ever gets a release that already passed on test.

## Signing in without a stored key

The deploy jobs sign in as the `storage-deployer` service account with CircleCI's short-lived OIDC token, through workload identity federation ([`infra/ci.tf`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/infra/ci.tf)). Google accepts the token only if it comes from our CircleCI project **and** from a pipeline on `hw2` or `main`. So:

- no Google key is stored in CircleCI, and nothing can leak from it;
- pull requests, other branches and forks can't get deploy credentials, even if they change `.circleci/config.yml`;
- the deployer can push images and deploy the two services, but can't read secret values, change IAM, or create or delete resources (see "Who can do what" in [Infrastructure](infrastructure.md)).

CircleCI only issues the token to jobs that use a context, which is why `release_image` and `deploy` use the context `gcp-deploy`. The context holds no variables.

### One-time setup

Done once by the project owner:

1. **Create the context.** CircleCI → Organization Settings → Contexts → **Create Context** → `gcp-deploy`. Optionally add an expression restriction `pipeline.git.branch == "hw2" or pipeline.git.branch == "main"`.
2. **Find the two IDs.** CircleCI → Organization Settings → Overview → **Organization ID**; Project Settings (OSPSD-Team-3) → Overview → **Project ID**. Neither is a secret.
3. **Add them to `infra/terraform.tfvars`** as `circleci_org_id` and `circleci_project_id`, then `tofu plan` (it should only add the identity pool, its provider, one IAM binding and two APIs) and `tofu apply`.

## Which version is running?

```bash
gcloud run services describe cloud-storage-service-demo --region=us-central1 --flatten="status.traffic[]" \
  --format="table(status.traffic.percent,status.traffic.revisionName,status.traffic.tag)" \
  | awk 'NR==1 || $1 ~ /^[0-9]+$/'
```

This prints only the revision that gets traffic. Old revisions keep their tags with no traffic, so the first traffic entry (`status.traffic[0]`) is often one of them. The tag is `rel-<first 7 characters of the commit SHA>`. Each revision also carries the label `release=<short sha>`, and its image is the digest built from that commit; the image itself sets the environment variable `RELEASE=<full sha>` at build time, so the running service can report its release. The CircleCI pipeline for that commit links the checks it passed.

## Roll back

Every release stays available as a tagged revision. To send traffic back to the previous good one (exercised in the [Rollback Drill](rollback-drill.md)):

```bash
gcloud run revisions list --service=cloud-storage-service-demo --region=us-central1
gcloud run services update-traffic cloud-storage-service-demo --region=us-central1 \
  --to-tags=rel-<previous short sha>=100
```

Then run the same smoke check against the service URL to confirm it works again:

```bash
uv run --no-project --with httpx python scripts/consumer_workflow.py <service url>
```

**What rollback can't undo:** it restores code and configuration, not data. Files a faulty release created, changed or deleted in the environment's Dropbox app folder stay as they are. To find them, search the service's logs for the faulty revision's requests (each log carries the revision) and compare with Dropbox's own file history; Dropbox keeps deleted files restorable for 30 days.

## If two pipelines run at once

Two pushes in quick succession each deploy their own revision with no traffic, and each moves traffic only after its own smoke check passes. Whichever finishes last ends up serving, which can be the older commit. Avoid merging twice within a few minutes; if it happens, re-run the newer pipeline's deploy job so the newest commit serves.
