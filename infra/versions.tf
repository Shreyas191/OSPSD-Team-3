# OpenTofu (or Terraform) and the Google provider. The provider version is pinned in
# .terraform.lock.hcl, which is committed so every apply uses the same provider build.

terraform {
  required_version = ">= 1.8"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # State lives in a versioned Cloud Storage bucket shared by the team, never on a laptop.
  # The bucket name is passed at init time (see docs/infrastructure.md):
  #   tofu init -backend-config="bucket=<state bucket>"
  backend "gcs" {
    prefix = "cloud-storage-service"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
