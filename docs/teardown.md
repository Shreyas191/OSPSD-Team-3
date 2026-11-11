# Teardown

This page covers HW2 Milestone 3, item 2.4: removing everything the team created for the deployed service, and confirming nothing is still costing money.

**When:** after HW2 grades are returned. The proposed date is **Dec 18, 2026** (DESIGN.md, "Deployment tools").
**Who:** the Google Cloud project owner, who also owns the Dropbox apps for `test` and `demo`.
**Status:** written on Nov 9, 2026; not run yet, because nothing is provisioned. A teammate who did not write it must run it from this page alone and record any question they had to ask (see the last section).

## 1. Destroy what OpenTofu created

From a clean checkout of the commit that was applied:

```bash
cd infra
tofu init -backend-config="bucket=<project-id>-tofu-state"
tofu plan -destroy -out=destroy.tfplan     # read it: only our resources, all marked "destroy"
tofu apply destroy.tfplan
```

This removes:

- both Cloud Run services and every revision;
- the Artifact Registry repository and every image in it;
- the six Dropbox secrets and all their versions;
- the three service accounts and every IAM binding they had.

Check that nothing is left:

```bash
tofu state list                                                   # prints nothing
gcloud run services list --region=us-central1                     # no cloud-storage-service-*
gcloud artifacts repositories list --location=us-central1         # no cloud-storage-service
gcloud secrets list --filter="labels.service=cloud-storage-service"   # 0 items
gcloud iam service-accounts list --filter="email~^storage-"       # 0 items
```

## 2. Delete the state bucket

OpenTofu can't delete the bucket that holds its own state. Once step 1 shows nothing left:

```bash
gcloud storage rm --recursive gs://<project-id>-tofu-state
```

`--recursive` also removes the old state versions kept by versioning.

## 3. Remove what lives outside OpenTofu

| What | How | Why it isn't in OpenTofu |
|---|---|---|
| CI credentials for the deployer (CircleCI context `gcp-deploy`, once item 2.6 adds it) | CircleCI → Organization Settings → Contexts → delete `gcp-deploy` | CircleCI settings aren't managed by our OpenTofu code |
| Dropbox apps for `test` and `demo` | Dropbox App Console → each app → Settings → Delete app. This also removes the app folders and all files in them. | Dropbox has no OpenTofu provider we use |
| Local credentials | `uv run python -m dropbox_client_impl logout`, then delete `.env` and `infra/terraform.tfvars` | They live only on each person's machine |
| Grafana Cloud stack (once item 3.1 adds it) | Grafana Cloud portal → the stack → Delete | Not provisioned by our OpenTofu code |

## 4. Delete the project

```bash
gcloud projects delete <project-id>
```

This removes anything left in the project, including the enabled APIs and the budget alert. Google keeps the project for 30 days before deleting it for good, and nothing in it is billed during that time.

## What intentionally stays

- **The CircleCI project itself.** HW1 already used it, and it costs nothing.
- **The GitHub repository and its releases.** They are the record of the work.
- **The billing account.** It may belong to a person and serve other projects; unlink it from this project only (step 4 does that), never close it.

## Confirm nothing costs money

Two days after teardown, the owner checks Billing → Reports for the project: cost for the last 2 days should be $0.00. The budget alert from setup would also email the owner if any spend appeared.

## Record of runs

Each run of this page goes here: date, who ran it, and every question they had to ask. A question is a gap in this page; fix the page in the same pull request.

| Date | Run by | Questions asked | Result |
|---|---|---|---|
| — | — | — | Not run yet |
