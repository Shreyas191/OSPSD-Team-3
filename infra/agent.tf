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
    # the agent from it, and the release label.
    ignore_changes = [
      scaling,
      template[0].revision,
      template[0].containers[0].image,
      template[0].containers[0].command,
      template[0].labels,
      client,
      client_version,
    ]
  }

  depends_on = [google_project_service.apis]
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
