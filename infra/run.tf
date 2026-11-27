# One Cloud Run service per environment. Each deploy creates a fixed revision, and rolling
# back means sending traffic to the previous revision (docs/infrastructure.md).

locals {
  impl = {
    for env in var.environments :
    env => lookup(var.vertical_implementations, env, { issue_tracker = "fake", calendar = "fake" })
  }
}

resource "google_cloud_run_v2_service" "service" {
  for_each = var.environments

  name     = "${local.service_name}-${each.key}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Teardown must be able to delete the service.
  deletion_protection = false

  template {
    service_account                  = google_service_account.runtime[each.key].email
    max_instance_request_concurrency = var.container_concurrency

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    dynamic "volumes" {
      for_each = local.impl[each.key].calendar == "team5" ? ["calendar-token"] : []
      content {
        name = volumes.value
        secret {
          secret = google_secret_manager_secret.calendar_token[each.key].secret_id
          items {
            version = "latest"
            path    = local.calendar_token_file
          }
        }
      }
    }

    containers {
      image = var.initial_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        # Only use CPU while a request is in flight; the cheapest setting.
        cpu_idle = true
      }

      env {
        name  = "MAX_IN_FLIGHT_REQUESTS"
        value = tostring(var.max_in_flight_requests)
      }

      # Telemetry (src/cloud_storage_service/.../telemetry.py): one JSON log line per
      # request, and traces of each request and its Dropbox calls in Cloud Trace.
      env {
        name  = "LOG_FORMAT"
        value = "json"
      }
      env {
        name  = "TRACE_EXPORTER"
        value = "gcp"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      # HW3 composition (composition.tf): which team's implementation runs.
      env {
        name  = "ISSUE_TRACKER_IMPL"
        value = local.impl[each.key].issue_tracker
      }
      env {
        name  = "CALENDAR_IMPL"
        value = local.impl[each.key].calendar
      }

      # Team 4's Trello package: mounted only when this environment runs it.
      dynamic "env" {
        for_each = local.impl[each.key].issue_tracker == "team4" ? local.trello_settings : toset([])
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.trello["${each.key}/${env.value}"].secret_id
              version = "latest"
            }
          }
        }
      }

      # Team 5's calendar package reads the token from a file; mounted only when it runs.
      dynamic "env" {
        for_each = local.impl[each.key].calendar == "team5" ? ["CALENDAR_TOKEN_FILE"] : []
        content {
          name  = env.value
          value = "${local.calendar_token_dir}/${local.calendar_token_file}"
        }
      }
      dynamic "volume_mounts" {
        for_each = local.impl[each.key].calendar == "team5" ? ["calendar-token"] : []
        content {
          name       = volume_mounts.value
          mount_path = local.calendar_token_dir
        }
      }

      # Google sign-in (sign_in.tf): only in environments given a redirect URI, or connecting
      # calendars (connections.tf).
      dynamic "env" {
        for_each = contains(local.google_oauth_envs, each.key) ? local.google_oauth_secrets : {}
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.google_oauth[env.key].secret_id
              version = "latest"
            }
          }
        }
      }
      dynamic "env" {
        for_each = contains(keys(var.sign_in_redirect_uris), each.key) ? ["SIGN_IN_REDIRECT_URI"] : []
        content {
          name  = env.value
          value = var.sign_in_redirect_uris[each.key]
        }
      }

      # The operator's execution switch (#102): approved proposals don't run while it's on;
      # reads, proposals and approvals keep working. See docs/deployment.md.
      dynamic "env" {
        for_each = contains(var.execution_disabled, each.key) ? ["EXECUTION"] : []
        content {
          name  = env.value
          value = "disabled"
        }
      }

      # People's provider connections (connections.tf): only in listed environments.
      dynamic "env" {
        for_each = contains(keys(var.connections), each.key) ? ["TOKEN_ENCRYPTION_KEY"] : []
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.token_encryption_key[each.key].secret_id
              version = "latest"
            }
          }
        }
      }
      dynamic "env" {
        for_each = lookup(local.connection_settings, each.key, {})
        content {
          name  = env.key
          value = env.value
        }
      }

      # HW3 product state (database.tf): this environment's Neon branch.
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url[each.key].secret_id
            version = "latest"
          }
        }
      }

      dynamic "env" {
        for_each = local.dropbox_secrets
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.dropbox["${each.key}/${env.value}"].secret_id
              version = "latest"
            }
          }
        }
      }

      # Startup: the port is open, which happens only after main.py's configuration check
      # passed. Liveness: GET /health, which never calls Dropbox, so a Dropbox outage does
      # not make Cloud Run restart a healthy container. Provider health is GET
      # /health/provider, which is for people and alerts, not for the platform.
      startup_probe {
        tcp_socket {
          port = 8080
        }
        period_seconds    = 2
        failure_threshold = 15
      }

      liveness_probe {
        http_get {
          path = "/health"
        }
        period_seconds    = 30
        failure_threshold = 3
      }
    }
  }

  lifecycle {
    # The pipeline deploys every release (a new image and its RELEASE label), not OpenTofu.
    # Ignoring them keeps a repeated apply from rolling the service back to initial_image.
    # Cloud Run also fills in a service-level scaling block with zeros after creation; our
    # limits are in template.scaling, so that block is Google's default, not drift.
    ignore_changes = [
      scaling,
      template[0].revision,
      template[0].containers[0].image,
      template[0].labels,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.runtime_reads_own_secrets,
    google_secret_manager_secret_iam_member.runtime_reads_trello,
    google_secret_manager_secret_iam_member.runtime_reads_calendar_token,
    google_secret_manager_secret_iam_member.runtime_reads_database_url,
    google_secret_manager_secret_iam_member.runtime_reads_google_oauth,
    google_secret_manager_secret_iam_member.runtime_reads_token_key,
  ]
}

# Without this, every caller would need a Google identity with run.invoker.
resource "google_cloud_run_v2_service_iam_member" "public" {
  for_each = var.public ? var.environments : toset([])

  name     = google_cloud_run_v2_service.service[each.key].name
  location = google_cloud_run_v2_service.service[each.key].location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
