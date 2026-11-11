# How CircleCI signs in to Google Cloud without a stored key.
#
# Every CircleCI job that uses a context gets a short-lived OIDC token signed by CircleCI.
# Workload identity federation trusts that token, but only from our CircleCI project and
# only for pipelines on the hw2 or main branch, and lets it act as the deployer service
# account. A pull request from another branch or a fork gets no Google credentials at all,
# and there is no long-lived key that could leak.

resource "google_iam_workload_identity_pool" "circleci" {
  workload_identity_pool_id = "circleci"
  display_name              = "CircleCI"
  description               = "OIDC tokens from the team's CircleCI pipelines."

  depends_on = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "circleci" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.circleci.workload_identity_pool_id
  workload_identity_pool_provider_id = "circleci"
  display_name                       = "CircleCI OIDC"

  oidc {
    issuer_uri        = "https://oidc.circleci.com/org/${var.circleci_org_id}"
    allowed_audiences = [var.circleci_org_id]
  }

  attribute_mapping = {
    # Google limits the subject to 127 bytes, and CircleCI's own `sub` claim is longer, so
    # the subject is the project ID and branch (about 55 bytes).
    "google.subject"       = "assertion['oidc.circleci.com/project-id'] + ':' + assertion['oidc.circleci.com/vcs-ref']"
    "attribute.project_id" = "assertion['oidc.circleci.com/project-id']"
    "attribute.vcs_ref"    = "assertion['oidc.circleci.com/vcs-ref']"
  }

  # Only our project, and only the branches that deploy. Tokens from any other branch,
  # fork or CircleCI project are rejected before they can be exchanged.
  attribute_condition = join(" && ", [
    "assertion['oidc.circleci.com/project-id'] == '${var.circleci_project_id}'",
    "assertion['oidc.circleci.com/vcs-ref'] in ${jsonencode([for b in var.deploy_branches : "refs/heads/${b}"])}",
  ])
}

# A token that passed the condition above may act as the deployer, and nothing else.
resource "google_service_account_iam_member" "circleci_is_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.circleci.name}/attribute.project_id/${var.circleci_project_id}"
}
