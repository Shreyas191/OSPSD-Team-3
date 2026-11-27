# HW3 Milestone 2: Google sign-in (#96).
#
# One OAuth client ("Web application", in the Google Cloud console of this project) serves
# every environment; each environment adds its own redirect URI to it. OpenTofu creates the
# two empty secrets and their access rules, and a person adds the values once without
# showing them, as for the other secrets:
#
#   read -rs "V?Client ID: " && printf %s "$V" \
#     | gcloud secrets versions add google-oauth-client-id --data-file=- ; unset V
#   read -rs "V?Client secret: " && printf %s "$V" \
#     | gcloud secrets versions add google-oauth-client-secret --data-file=- ; unset V
#
# Then list the environment in sign_in_redirect_uris. Only listed environments read the
# secrets (run.tf), so a service never references a secret that has no value yet; the
# others answer 503 sign_in_not_configured on the sign-in routes.

locals {
  google_oauth_secrets = {
    GOOGLE_OAUTH_CLIENT_ID     = "google-oauth-client-id"
    GOOGLE_OAUTH_CLIENT_SECRET = "google-oauth-client-secret"
  }
  google_oauth_readers = {
    for pair in setproduct(local.google_oauth_envs, keys(local.google_oauth_secrets)) :
    "${pair[0]}/${pair[1]}" => { env = pair[0], name = pair[1] }
  }
}

resource "google_secret_manager_secret" "google_oauth" {
  for_each = local.google_oauth_secrets

  secret_id = each.value

  replication {
    auto {}
  }

  labels = {
    service   = local.service_name
    component = "sign-in"
  }

  depends_on = [google_project_service.apis]
}

# Only the API service of an environment with sign-in or calendar connections on reads the
# client (connections.tf); never the agent.
resource "google_secret_manager_secret_iam_member" "runtime_reads_google_oauth" {
  for_each = local.google_oauth_readers

  secret_id = google_secret_manager_secret.google_oauth[each.value.name].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime[each.value.env].member
}
