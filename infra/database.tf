# HW3 Milestone 2: the persisted database (Neon Postgres, project "course-autopilot").
#
# Each environment has its own Neon branch: test uses the "test" branch and demo uses
# "production", so test data never mixes with what demo users see.
#
# Same pattern as secrets.tf: OpenTofu creates the empty secret and its access rule, and a
# person adds the value once, piping the Neon CLI straight into Secret Manager so the
# connection string (it holds the database password) never appears on screen, in a file,
# in this code, the plan or the state:
#
#   neon connection-string <branch> --project-id <id> --pooled \
#     | gcloud secrets versions add database-url-<env> --data-file=-
#
# Nothing reads these secrets yet; the services mount them when the database layer lands.

resource "google_secret_manager_secret" "database_url" {
  for_each = var.environments

  # e.g. database-url-test
  secret_id = "database-url-${each.key}"

  replication {
    auto {}
  }

  labels = {
    service     = local.service_name
    environment = each.key
    component   = "database"
  }

  depends_on = [google_project_service.apis]
}

# Each environment's runtime identity reads only its own database URL.
resource "google_secret_manager_secret_iam_member" "runtime_reads_database_url" {
  for_each = var.environments

  secret_id = google_secret_manager_secret.database_url[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime[each.key].member
}
