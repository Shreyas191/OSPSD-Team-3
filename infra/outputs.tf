output "service_urls" {
  description = "Base URL of each environment's service."
  value       = { for env, svc in google_cloud_run_v2_service.service : env => svc.uri }
}

output "image_repository" {
  description = "Where the pipeline pushes images: <this>/cloud-storage-service:<commit SHA>."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "workload_identity_provider" {
  description = "Full name of the CircleCI provider, used by the deploy job to sign in."
  value       = google_iam_workload_identity_pool_provider.circleci.name
}

output "deployer_service_account" {
  description = "Identity the CI pipeline deploys with."
  value       = google_service_account.deployer.email
}

output "runtime_service_accounts" {
  description = "Identity each environment's service runs as."
  value       = { for env, sa in google_service_account.runtime : env => sa.email }
}
