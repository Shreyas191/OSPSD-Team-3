# HW3 Milestone 2: people's own provider connections (#98-#101).
#
# Each environment has its own Fernet key, which encrypts the tokens stored in
# provider_connections. OpenTofu creates the empty secret; a person generates the key and
# stores it without it ever appearing on screen:
#
#   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode(), end='')" \
#     | gcloud secrets versions add token-encryption-key-<env> --data-file=-
#
# Then list the environment in var.connections with the return URL of each provider it
# offers. Only listed environments read the key (run.tf), so a service never references a
# secret that has no value yet; the others answer 503 connections_not_configured.
#
# A provider is offered only when its return URL is set:
#   - Dropbox: the API's own app (DROPBOX_APP_KEY / DROPBOX_APP_SECRET, App folder access),
#     with this redirect URI added to the app in the Dropbox App Console.
#   - Google Calendar: the sign-in OAuth client (sign_in.tf), with this redirect URI added
#     to it. Setting it mounts the client's secrets even without sign-in.
#   - Trello: the Power-Up's API key (public: it appears in the browser's authorize URL),
#     with the return URL's origin allowed on the Power-Up.

resource "google_secret_manager_secret" "token_encryption_key" {
  for_each = var.environments

  # e.g. token-encryption-key-test
  secret_id = "token-encryption-key-${each.key}"

  replication {
    auto {}
  }

  labels = {
    service     = local.service_name
    environment = each.key
    component   = "connections"
  }

  depends_on = [google_project_service.apis]
}

# Only the API service of an environment with connections on reads its own key; never the
# agent, which reaches providers only through the API service.
resource "google_secret_manager_secret_iam_member" "runtime_reads_token_key" {
  for_each = var.connections

  secret_id = google_secret_manager_secret.token_encryption_key[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime[each.key].member
}

locals {
  # Plain settings each listed environment gets, when set (connectors.py reads them).
  connection_settings = {
    for env, c in var.connections : env => {
      for name, value in {
        DROPBOX_CONNECT_REDIRECT_URI  = c.dropbox_redirect_uri
        CALENDAR_CONNECT_REDIRECT_URI = c.calendar_redirect_uri
        TRELLO_CONNECT_API_KEY        = c.trello_return_url != "" ? c.trello_api_key : ""
        TRELLO_CONNECT_RETURN_URL     = c.trello_api_key != "" ? c.trello_return_url : ""
      } : name => value if value != ""
    }
  }
  # Environments whose API reads the Google OAuth client: sign-in, or calendar connections.
  google_oauth_envs = setunion(
    keys(var.sign_in_redirect_uris),
    [for env, c in var.connections : env if c.calendar_redirect_uri != ""],
  )
}
