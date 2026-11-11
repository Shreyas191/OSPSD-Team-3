# One secret per (environment, Dropbox setting). OpenTofu creates the empty secrets and their
# access rules; the account owner adds each value once with `gcloud secrets versions add`
# (docs/infrastructure.md). Values therefore never appear in this code, the plan or the state.

resource "google_secret_manager_secret" "dropbox" {
  for_each = local.env_secrets

  # e.g. dropbox-app-key-test (the names docs/deployment.md lists)
  secret_id = "${lower(replace(each.value.name, "_", "-"))}-${each.value.env}"

  replication {
    auto {}
  }

  labels = {
    service     = local.service_name
    environment = each.value.env
  }

  depends_on = [google_project_service.apis]
}

# Each runtime identity reads only its own environment's three secrets.
resource "google_secret_manager_secret_iam_member" "runtime_reads_own_secrets" {
  for_each = local.env_secrets

  secret_id = google_secret_manager_secret.dropbox[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime[each.value.env].member
}
