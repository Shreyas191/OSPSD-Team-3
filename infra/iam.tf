# Two kinds of identity, each with only what its job needs:
#
# - runtime (one per environment): the identity each Cloud Run service runs as. It can read
#   its own environment's three Dropbox secrets and nothing else, so the test service can't
#   reach the demo service's Dropbox folder.
# - deployer (one): the identity the pipeline deploys with. It can push images and deploy
#   new revisions of the two services. It can't read secret values or change IAM, so a leaked
#   CI credential can ship a bad image but can't read the Dropbox credentials.

resource "google_service_account" "runtime" {
  for_each = var.environments

  account_id   = "storage-run-${each.key}"
  display_name = "Cloud storage service runtime (${each.key})"
}

resource "google_service_account" "deployer" {
  account_id   = "storage-deployer"
  display_name = "Cloud storage service deployer (CI)"
}

# Push images to our repository only.
resource "google_artifact_registry_repository_iam_member" "deployer_writes_images" {
  repository = google_artifact_registry_repository.images.name
  location   = google_artifact_registry_repository.images.location
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer.member
}

# Deploy new revisions of our two services only (not create, delete or re-permission them).
resource "google_cloud_run_v2_service_iam_member" "deployer_deploys" {
  for_each = var.environments

  name     = google_cloud_run_v2_service.service[each.key].name
  location = google_cloud_run_v2_service.service[each.key].location
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

# A deploy must run the new revision as the environment's runtime identity, which needs
# permission to act as that identity, and nothing more.
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  for_each = var.environments

  service_account_id = google_service_account.runtime[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}
