# HW3 Milestone 2: the interface, one per environment, next to the agent and API services.
#
# It runs the same image as the other two services (the pipeline deploys one image to all
# three), with web_main.py as its entry point. Its runtime identity can read no secret: the
# interface holds only the browser's cookies and gets everything else from the API service
# (DESIGN.md, P4).

# Cloud Run serves a service at two addresses: a hash-based one (its `uri`) and a deterministic
# one, https://SERVICE-PROJECT_NUMBER.REGION.run.app, known before the service exists. Cookies
# belong to one host, and Google, Dropbox and Trello match redirect URIs exactly, so the
# interface uses one: the deterministic address. The service is told it (PUBLIC_URL; it can't
# read its own `uri`), sends page requests on the other address there, and the
# `web_service_urls` output gives it for sign_in_redirect_uris and the connections' redirects.
data "google_project" "current" {
  project_id = var.project_id
}

locals {
  web_urls = {
    for env in var.environments :
    env => "https://course-web-${env}-${data.google_project.current.number}.${var.region}.run.app"
  }
}

resource "google_service_account" "web_runtime" {
  for_each = var.environments

  account_id   = "web-run-${each.key}"
  display_name = "Course Autopilot interface runtime (${each.key})"
}

resource "google_cloud_run_v2_service" "web" {
  for_each = var.environments

  name     = "course-web-${each.key}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  deletion_protection = false

  template {
    service_account = google_service_account.web_runtime[each.key].email

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      # As for the agent service (agent.tf): the placeholder image runs until the first
      # release, and the pipeline sets our image and `python web_main.py` (deploy_web.sh).
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

      # The only service the interface calls in this slice; sign-in and every check happen
      # there (DESIGN.md, P4).
      env {
        name  = "API_BASE_URL"
        value = google_cloud_run_v2_service.service[each.key].uri
      }

      # Google sends the browser back to <PUBLIC_URL>/auth/callback, which must be listed in
      # sign_in_redirect_uris for the API service (the web_service_urls output).
      env {
        name  = "PUBLIC_URL"
        value = local.web_urls[each.key]
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
    # Same as the other services: the pipeline owns the image, the start command and the
    # release label.
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

  depends_on = [google_project_service.apis]
}

# Students open the interface in their browser, so it is public. Sign-in (#96) decides who
# can do anything beyond the start page.
resource "google_cloud_run_v2_service_iam_member" "web_public" {
  for_each = var.public ? var.environments : toset([])

  name     = google_cloud_run_v2_service.web[each.key].name
  location = google_cloud_run_v2_service.web[each.key].location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# The pipeline deploys new revisions of the interface, as it does for the other services.
resource "google_cloud_run_v2_service_iam_member" "deployer_deploys_web" {
  for_each = var.environments

  name     = google_cloud_run_v2_service.web[each.key].name
  location = google_cloud_run_v2_service.web[each.key].location
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_acts_as_web_runtime" {
  for_each = var.environments

  service_account_id = google_service_account.web_runtime[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}
