# Infrastructure (OpenTofu)

This page covers HW2 Milestone 3, Level 1, item 2.3: the cloud resources the service runs on, declared in [`infra/`](https://github.com/Shreyas191/OSPSD-Team-3/tree/hw2/infra) and created with OpenTofu. The container image is described in [Container and Configuration](deployment.md), and removing everything in [Teardown](teardown.md).

**Status: applied on Nov 11, 2026** to the team's project `ospsd-team3` (owner: Shreyas Kaldate), in two steps as described below: 10 resources, then 19. A second `tofu plan` reported "No changes", and both services answered `GET /health` with `200`.

## What gets created, and why

The service keeps no state of its own; every file lives in Dropbox. So it needs only what runs the image, keeps the image, and holds the credentials.

| Resource | Count | What it is for | What breaks without it |
|---|---|---|---|
| Google APIs (`run`, `artifactregistry`, `secretmanager`, `iam`) | 4 | Lets the project use the services below | Nothing else can be created |
| Artifact Registry repository `cloud-storage-service` | 1 | Holds one image per commit, tagged with the SHA; keeps the 5 newest | The pipeline has nowhere to push, Cloud Run nothing to pull |
| Cloud Run service `cloud-storage-service-test` | 1 | Disposable environment for the faulty-release and alert exercises | Those exercises would have to break the demo service |
| Cloud Run service `cloud-storage-service-demo` | 1 | The service we demonstrate and graders call | No deployed API |
| Runtime service account per environment (`storage-run-test`, `storage-run-demo`) | 2 | Identity each service runs as; reads only its own 3 secrets | The services would share one identity, so test could read demo's Dropbox credentials |
| Deployer service account (`storage-deployer`) | 1 | Identity CI deploys with: push images, deploy revisions of these two services, nothing else | CI would need a person's or the owner's credentials |
| Secrets (`dropbox-app-key-<env>`, `dropbox-app-secret-<env>`, `dropbox-refresh-token-<env>`) | 6 | Dropbox credentials, read by Cloud Run when an instance starts | The service stops at startup ("Startup failed: ... missing DROPBOX_...") |
| Public invoker binding (`allUsers` → `run.invoker`) | 2 | Graders and the conformance suite can call the API | Every caller needs a Google identity |

Settings that matter (all in [`infra/variables.tf`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/infra/variables.tf)):

- **Concurrency 20 per instance, our limit 10.** Cloud Run sends up to 20 requests to an instance; `MAX_IN_FLIGHT_REQUESTS=10` makes the service answer the rest with our own `503 service_busy` body instead of a Cloud Run error.
- **At most 2 instances, at least 0.** Bounds cost and the rate of calls to Dropbox; with no traffic the service scales to zero and costs nothing.
- **Probes.** Startup: the port opens, which only happens after `main.py`'s configuration check passes. Liveness: `GET /health`, which never calls Dropbox, so a Dropbox outage does not make Cloud Run restart a healthy container.
- **Image is not managed by OpenTofu after the first apply.** A new service starts with Google's `hello` image; from then on the pipeline deploys each release by digest, and `run.tf` ignores the image, so a repeated `tofu apply` never rolls a release back.

### Who can do what

| Identity | Can | Can't |
|---|---|---|
| `storage-run-<env>` (the running service) | Read its own environment's 3 secrets | Read the other environment's secrets, deploy, change anything |
| `storage-deployer` (CI) | Push images to our repository; deploy revisions of the two services; run them as their runtime identity | Read secret values, change IAM, create or delete services, touch state |
| The project owner (a person) | Everything, including `tofu apply` and adding secret values | — |

### Blast radius

- **Test can't affect demo.** Each has its own runtime identity, its own three secrets and its own Dropbox app, so code running on test can't read demo's credentials or files. Teammates who run experiments get deploy rights on test only. A release reaches demo only after the same release passed its smoke check on test (`deploy_demo` requires `deploy_test`), so a release that breaks the contract stops at test. What the two do share: this project's quotas and budget, the image repository, the dashboard and the alert.
- **What a leaked CI credential could do.** No Google key is stored; CircleCI gets a short-lived token, and only for pipelines on `hw2` or `main` of our project ([Release Pipeline](release-pipeline.md#signing-in-without-a-stored-key)). Whoever can push to those branches can deploy any image to either service. That image runs as that environment's runtime identity, so it could read that environment's Dropbox token and read, change or delete files in **that environment's Dropbox app folder**. It can't change IAM, infrastructure or the state bucket, create or delete services, or reach other Dropbox folders or other projects.
- **Worst case of one bad merge to `hw2`.** It passes CI and the smoke check (which only exercise the contract) and is deployed to test and then demo. The worst it could do is damage or leak the data in the two Dropbox app folders. Recovery is a rollback ([Release Pipeline](release-pipeline.md), "Roll back") and, if credentials may have leaked, a token rotation ([Runbook](runbook.md#dropbox-rejects-our-credentials-502-provider_error)). Neither restores files the bad release already changed in Dropbox.

## One-time setup by hand

These steps can't be automated, because they create the account and permissions everything else needs. The project owner does them once.

1. **Create the project and link billing.** In the Google Cloud console, create a project for the team (not a personal or work project) and link a billing account. The free tiers still require one.
2. **Add a budget alert:** Billing → Budgets & alerts → $5 for this project, emailing the owner.
3. **Install OpenTofu** (≥ 1.8) and the `gcloud` CLI, and log in with the account that owns the project:

   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project <project-id>
   ```

4. **Create the state bucket.** State is shared by the team and versioned, so a bad apply can be undone. It isn't created by OpenTofu itself, because OpenTofu needs it before it can run:

   ```bash
   gcloud storage buckets create gs://<project-id>-tofu-state \
     --location=us-central1 --uniform-bucket-level-access --public-access-prevention
   gcloud storage buckets update gs://<project-id>-tofu-state --versioning
   ```

   Only the owner's account (and teammates the owner adds as Storage Admin on this bucket) can read or change it. State can contain resource details, so never copy it into the repository or a CI artifact.

## Provision

**What you need first:**

- OpenTofu 1.8 or newer ([install](https://opentofu.org/docs/intro/install/)), and the `gcloud` CLI signed in with the three commands in step 3 above.
- `jq` for the check at the end, and `uv` for the Dropbox login ([README, Project Setup](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/README.md#project-setup)).
- Access. In the team's project (`ospsd-team3`), ask the owner for the Owner role on the project and Storage Admin on the state bucket: the apply changes IAM, which the Editor role can't do. To build a copy of your own, do "One-time setup by hand" in your own project.

From a clean checkout:

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars     # fill in all four values; this file is git-ignored
tofu init -backend-config="bucket=<project-id>-tofu-state"
```

`terraform.tfvars` needs four values. None is a secret.

| Value | Where it comes from |
|---|---|
| `project_id` | The Google Cloud project's ID, for the team `ospsd-team3` |
| `circleci_org_id`, `circleci_project_id` | CircleCI's settings pages: step 2 of "One-time setup" in [Release Pipeline](release-pipeline.md#one-time-setup) |
| `alert_email` | The address that receives the 5xx alert |

In the team's project, use the values of the last apply. `alert_email` is not in the repository, so ask the owner for it. A different value shows in `tofu plan` as a change.

The secrets must have a value before Cloud Run can start a revision that reads them, so apply in two steps:

1. **Create the APIs and the empty secrets, then add the values.**

   ```bash
   tofu plan  -target=google_secret_manager_secret.dropbox -out=secrets.tfplan
   tofu apply secrets.tfplan
   ```

   OpenTofu warns that resource targeting is in effect and that the changes may be incomplete. For this step that is expected.

   For each environment, log in to that environment's Dropbox app and add its three values, piped so they never appear on the command line or in shell history.

   - Run these from the repository root (`cd ..`), not from `infra/`: the last command reads `.dropbox_token.json` there.
   - The app key and secret are on that app's Settings tab in the Dropbox App Console. Put them in `.env`, run `uv run python -m dropbox_client_impl login`, and set the same two values in your shell (for example `read -rs DROPBOX_APP_KEY`, then paste, which keeps the value out of shell history).
   - Each environment has its own Dropbox app, and a second login overwrites `.dropbox_token.json`. Add all three values for `test` before you log in for `demo`.

   ```bash
   ENV=test   # then demo
   printf '%s' "$DROPBOX_APP_KEY"    | gcloud secrets versions add dropbox-app-key-$ENV --data-file=-
   printf '%s' "$DROPBOX_APP_SECRET" | gcloud secrets versions add dropbox-app-secret-$ENV --data-file=-
   uv run python -c 'import json; print(json.load(open(".dropbox_token.json"))["refresh_token"], end="")' \
     | gcloud secrets versions add dropbox-refresh-token-$ENV --data-file=-
   ```

2. **Create everything else.** Back in `infra/`, read the plan before applying it; it should only add resources. The counts in the status line at the top are from the first apply. The pipeline's sign-in, the alert, the metrics, the dashboard and the probe were added to `infra/` after it, so an apply from nothing now creates more.

   ```bash
   tofu plan -out=all.tfplan
   tofu apply all.tfplan
   tofu output service_urls
   ```

Saved plans (`*.tfplan`) and `terraform.tfvars` are git-ignored; delete the plans after applying.

## Check it

```bash
URL=$(tofu output -json service_urls | jq -r .demo)
curl -s "$URL/health"                   # {"status":"ok"} once a release is deployed
curl -s "$URL/files?path=/"             # the shared API: 200 and a JSON array of the folder's entries
tofu plan                               # must report: No changes.
```

A second `plan` with unchanged inputs must show **no changes**. If it shows any, something outside OpenTofu changed a managed setting; read the diff before applying.

Until the pipeline (item 2.6) deploys a release, the services run Google's `hello` image, which answers every path with a placeholder page. The pipeline deploys on every push to `hw2`, and it is set up for the team's project (`GCP_PROJECT_ID` in `.circleci/config.yml`), so a copy in another project gets no release from it. See [Release Pipeline](release-pipeline.md).

## Roll back a release

Each deploy creates a new Cloud Run revision; the previous one stays. To send all traffic back to it:

```bash
gcloud run revisions list --service=cloud-storage-service-demo --region=us-central1
gcloud run services update-traffic cloud-storage-service-demo --region=us-central1 \
  --to-revisions=<previous revision>=100
```

A rollback restores code and configuration only. Files a faulty release already wrote to Dropbox stay as they are.

## Changing the infrastructure

### Changing a Cloud Run service's template

The pipeline names every revision it deploys, and `infra/run.tf` ignores that name so pipeline deploys don't show up as drift. When OpenTofu itself has to change the revision template (an environment variable, a probe, a resource limit), it would send the current revision's name with a new configuration, and Google refuses with `409 Revision named ... with different configuration already exists`. For that one apply:

1. Remove the line `template[0].revision,` from `ignore_changes` in `infra/run.tf` (don't commit this).
2. `tofu plan -out=x.tfplan` shows `revision = "<current name>" -> null` plus your change; `tofu apply x.tfplan`. Google names the new revision itself. It gets no traffic: traffic stays on the tagged release until the next deploy, which builds on the new template.
3. Put the line back, and check `tofu plan` reports **No changes**.

This happened on Nov 11 when the telemetry settings (`LOG_FORMAT`, `TRACE_EXPORTER`, `GOOGLE_CLOUD_PROJECT`) were added.


Edit `infra/`, run `tofu fmt` and `tofu validate`, and open a pull request with the output of `tofu plan` in its description. Apply only after review, from the reviewed commit.

## Teammate provisioning record

Rubric 13.1: a teammate who did not write `infra/` follows this page from a fresh checkout, provisions the infrastructure, reaches the shared API through the deployed service, and records every question they had to ask. Each question is a gap in this page; fix it in the same pull request.

| Date | Teammate | Project | Provisioned without help | Shared API reached | Questions they had to ask |
|---|---|---|---|---|---|
| — | Not run yet | — | — | — | — |

Gaps found on a read-through of this page from a fresh checkout on Nov 12, 2026, before any run. Nothing was provisioned for it. The page now answers each one:

| Question | Where the answer is now |
|---|---|
| The page said to set `project_id`, but `terraform.tfvars.example` has four values. Which are needed, and where do they come from? | The table under "Provision" |
| Can a teammate who isn't the project owner run this, and with what access? | "What you need first" |
| [Teardown](teardown.md) said `terraform.tfvars` needs three values. Which is right? | Teardown, step 1, now lists the same four |
| Which tools are needed besides OpenTofu and `gcloud`? | "What you need first" |
| Where do `$DROPBOX_APP_KEY`, `$DROPBOX_APP_SECRET` and `.dropbox_token.json` come from, and which directory are the secret commands run in? | The list under step 1 |
| Is the "resource targeting" warning in step 1 a problem? | Step 1 |
| The status line says 10 resources, then 19. Is a longer plan wrong? | Step 2 |
| `GET /health` shows the service is up. How do I reach the shared API itself, and how does a release get onto the service? | "Check it" |
