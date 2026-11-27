# Launch week: a daily logical backup of the database (docs/database-backup.md; #120).
#
# Neon's free plan can restore a branch only within the last 6 hours. A Cloud Run job runs
# `pg_dump` once a day and writes the dump to a private bucket that deletes it after 14 days.
# Only environments listed in var.database_backup_environments get a job and a schedule.
#
# The job runs backup_main.py from the same image as the services. OpenTofu starts it on the
# image the API service is serving at apply time; after that, each release points it at the
# new image (scripts/deploy_backup_job.sh). So list an environment only once a release that
# includes backup_main.py is deployed there.

locals {
  backup_bucket = "${var.project_id}-database-backups"
}

resource "google_storage_bucket" "database_backups" {
  count = length(var.database_backup_environments) > 0 ? 1 : 0

  name                        = local.backup_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  # Holding backups is the point: `tofu destroy` must not delete them unnoticed. Teardown
  # empties the bucket first (docs/database-backup.md).
  force_destroy = false

  # The dumps hold students' conversations: keep them 14 days, then delete them for good.
  lifecycle_rule {
    condition {
      age = var.database_backup_retention_days
    }
    action {
      type = "Delete"
    }
  }
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  labels = {
    service   = local.service_name
    component = "database-backup"
  }

  depends_on = [google_project_service.apis]
}

# The job's identity: reads its environment's database URL and creates objects, nothing
# else. It can't read, replace or delete a backup (objectCreator), so a compromised job
# can't destroy earlier ones.
resource "google_service_account" "database_backup" {
  for_each = var.database_backup_environments

  account_id   = "db-backup-${each.key}"
  display_name = "Database backup job (${each.key})"
}

resource "google_secret_manager_secret_iam_member" "backup_reads_database_url" {
  for_each = var.database_backup_environments

  secret_id = google_secret_manager_secret.database_url[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.database_backup[each.key].member
}

resource "google_storage_bucket_iam_member" "backup_writes_dumps" {
  for_each = var.database_backup_environments

  bucket = google_storage_bucket.database_backups[0].name
  role   = "roles/storage.objectCreator"
  member = google_service_account.database_backup[each.key].member
}

resource "google_cloud_run_v2_job" "database_backup" {
  for_each = var.database_backup_environments

  name     = "database-backup-${each.key}"
  location = var.region

  deletion_protection = false

  template {
    task_count = 1

    template {
      service_account = google_service_account.database_backup[each.key].email
      max_retries     = 1
      timeout         = "900s"

      containers {
        image   = google_cloud_run_v2_service.service[each.key].template[0].containers[0].image
        command = ["python"]
        args    = ["backup_main.py"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name  = "BACKUP_BUCKET"
          value = google_storage_bucket.database_backups[0].name
        }
        env {
          name  = "BACKUP_PREFIX"
          value = each.key
        }
        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url[each.key].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # The pipeline points the job at each release's image (deploy_backup_job.sh).
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.backup_reads_database_url,
    google_storage_bucket_iam_member.backup_writes_dumps,
  ]
}

# Cloud Scheduler starts the job once a day, as its own identity, which may only run it.
resource "google_service_account" "backup_scheduler" {
  count = length(var.database_backup_environments) > 0 ? 1 : 0

  account_id   = "db-backup-scheduler"
  display_name = "Starts the database backup jobs"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_backup" {
  for_each = var.database_backup_environments

  name     = google_cloud_run_v2_job.database_backup[each.key].name
  location = google_cloud_run_v2_job.database_backup[each.key].location
  role     = "roles/run.invoker"
  member   = google_service_account.backup_scheduler[0].member
}

resource "google_cloud_scheduler_job" "database_backup" {
  for_each = var.database_backup_environments

  name             = "database-backup-${each.key}"
  region           = var.region
  description      = "Daily pg_dump of the ${each.key} database (docs/database-backup.md)"
  schedule         = var.database_backup_schedule
  time_zone        = "America/New_York"
  attempt_deadline = "320s"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.database_backup[each.key].name}:run"

    oauth_token {
      service_account_email = google_service_account.backup_scheduler[0].email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_runs_backup]
}

# The pipeline updates the job's image with each release, as it does for the services.
resource "google_cloud_run_v2_job_iam_member" "deployer_updates_backup" {
  for_each = var.database_backup_environments

  name     = google_cloud_run_v2_job.database_backup[each.key].name
  location = google_cloud_run_v2_job.database_backup[each.key].location
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_acts_as_backup" {
  for_each = var.database_backup_environments

  service_account_id = google_service_account.database_backup[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}
