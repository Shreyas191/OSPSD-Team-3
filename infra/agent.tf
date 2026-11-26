# HW3 Milestone 2: the agent service, one per environment, next to the API service.
#
# It runs the same image as the API service (the pipeline deploys one image to both), with
# agent_main.py as its entry point. It has its own runtime identity that can read no Dropbox,
# Trello or calendar secret: the agent reaches providers only through the API service.

resource "google_service_account" "agent_runtime" {
  for_each = var.environments

  account_id   = "agent-run-${each.key}"
  display_name = "Course Autopilot agent service runtime (${each.key})"
}

resource "google_cloud_run_v2_service" "agent" {
  for_each = var.environments

  name     = "course-agent-${each.key}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  deletion_protection = false

  template {
    service_account = google_service_account.agent_runtime[each.key].email

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      # The placeholder image runs its own server until the first release. The pipeline
      # deploys our image with `python agent_main.py` as the command (deploy_agent.sh), so
      # the command, like the image, is set by the pipeline and ignored here.
      image = var.initial_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      # disabled until the Gemini key is in Secret Manager; see variables.tf.
      env {
        name  = "MODEL_PROVIDER"
        value = lookup(var.agent_model_provider, each.key, "disabled")
      }

      # The model's key, mounted only when this environment runs Gemini.
      dynamic "env" {
        for_each = lookup(var.agent_model_provider, each.key, "disabled") == "gemini" ? ["GEMINI_API_KEY"] : []
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.gemini_api_key[each.key].secret_id
              version = "latest"
            }
          }
        }
      }

      # The only service the agent calls.
      env {
        name  = "API_BASE_URL"
        value = google_cloud_run_v2_service.service[each.key].uri
      }

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
    # Same as the API service (run.tf): the pipeline owns the image, the command that starts
    # the agent from it (command and args), and the release label.
    ignore_changes = [
      scaling,
      template[0].revision,
      template[0].containers[0].image,
      template[0].containers[0].command,
      template[0].containers[0].args,
      template[0].labels,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.agent_reads_gemini_api_key,
  ]
}

# The interface calls the agent service directly. Sign-in (Level 4) adds per-user
# authentication inside the service; until then it is public like the API service.
resource "google_cloud_run_v2_service_iam_member" "agent_public" {
  for_each = var.public ? var.environments : toset([])

  name     = google_cloud_run_v2_service.agent[each.key].name
  location = google_cloud_run_v2_service.agent[each.key].location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# The pipeline deploys new revisions of the agent services, as it does for the API services.
resource "google_cloud_run_v2_service_iam_member" "deployer_deploys_agent" {
  for_each = var.environments

  name     = google_cloud_run_v2_service.agent[each.key].name
  location = google_cloud_run_v2_service.agent[each.key].location
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_acts_as_agent_runtime" {
  for_each = var.environments

  service_account_id = google_service_account.agent_runtime[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}

# The Gemini API key, one secret per environment. Same pattern as database.tf: OpenTofu
# creates the empty secret and its access rule; a person adds the value from a hidden prompt,
# so the key never appears on screen, in shell history, in this code, the plan or the state:
#
#   read -rs GEMINI_KEY && printf %s "$GEMINI_KEY" \
#     | gcloud secrets versions add gemini-api-key-<env> --data-file=- ; unset GEMINI_KEY
#
# Then set agent_model_provider = { <env> = "gemini" } and apply, which mounts it.
resource "google_secret_manager_secret" "gemini_api_key" {
  for_each = var.environments

  secret_id = "gemini-api-key-${each.key}"

  replication {
    auto {}
  }

  labels = {
    service     = "course-agent"
    environment = each.key
    component   = "model"
  }

  depends_on = [google_project_service.apis]
}

# Only the agent service reads the model key; the API service never sees it.
resource "google_secret_manager_secret_iam_member" "agent_reads_gemini_api_key" {
  for_each = var.environments

  secret_id = google_secret_manager_secret.gemini_api_key[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.agent_runtime[each.key].member
}
