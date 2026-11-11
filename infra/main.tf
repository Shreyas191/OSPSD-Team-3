# What the service needs, and nothing else: Google APIs, one image registry, two service
# accounts per purpose (see iam.tf), three secrets per environment (secrets.tf), and one
# Cloud Run service per environment (run.tf). The service keeps no state of its own; every
# file lives in Dropbox.

locals {
  service_name = "cloud-storage-service"

  # The three settings dropbox_client_impl.auth reads from the environment. Their values
  # live only in Secret Manager; OpenTofu creates the secrets but never their values.
  dropbox_secrets = toset(["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"])

  # One entry per (environment, secret), e.g. "test/DROPBOX_APP_KEY".
  env_secrets = {
    for pair in setproduct(var.environments, local.dropbox_secrets) :
    "${pair[0]}/${pair[1]}" => { env = pair[0], name = pair[1] }
  }
}

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
  ])

  service = each.value
  # Teardown removes our resources but leaves the APIs on; turning an API off can affect
  # anything else in the project.
  disable_on_destroy = false
}

# Where the pipeline pushes the image it builds once per release, tagged with the commit SHA.
resource "google_artifact_registry_repository" "images" {
  repository_id = local.service_name
  location      = var.region
  format        = "DOCKER"
  description   = "Images of the cloud storage service, one per commit."

  # Images are ~53 MB and the free tier is 0.5 GB, so keep only the last 5 versions.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      tag_state = "ANY"
    }
  }

  depends_on = [google_project_service.apis]
}
