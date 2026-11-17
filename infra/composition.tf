# HW3 composition: the credentials other teams' implementations need, and which
# implementation each environment runs (src/cloud_storage_service/.../verticals.py).
#
# Same pattern as secrets.tf: OpenTofu creates empty secrets and their access rules, a
# person adds each value with `gcloud secrets versions add` (docs/composed-workflow.md), and
# values never appear in this code, the plan or the state.
#
# Order matters. A secret must have a value before a revision that reads it can start, so:
#   1. apply with the default implementations (`fake`): creates the empty secrets only;
#   2. add the values;
#   3. set `vertical_implementations` to team4/team5 for the environment and apply again,
#      which mounts the secrets and switches the implementation.

locals {
  # Settings Team 4's Trello package reads from the environment (our own Trello test board).
  trello_settings = toset(["TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_LIST_ID"])

  # Where Team 5's calendar package finds the OAuth token file of our Google test account.
  calendar_token_dir  = "/secrets/calendar"
  calendar_token_file = "token.json"

  # e.g. "test/TRELLO_TOKEN", one per (composition environment, Trello setting).
  trello_env_secrets = {
    for pair in setproduct(var.composition_environments, local.trello_settings) :
    "${pair[0]}/${pair[1]}" => { env = pair[0], name = pair[1] }
  }
}

resource "google_secret_manager_secret" "trello" {
  for_each = local.trello_env_secrets

  # e.g. trello-token-test
  secret_id = "${lower(replace(each.value.name, "_", "-"))}-${each.value.env}"

  replication {
    auto {}
  }

  labels = {
    service     = local.service_name
    environment = each.value.env
    vertical    = "issue-tracker"
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "calendar_token" {
  for_each = var.composition_environments

  # e.g. calendar-token-test: the whole token.json, mounted as a file.
  secret_id = "calendar-token-${each.key}"

  replication {
    auto {}
  }

  labels = {
    service     = local.service_name
    environment = each.key
    vertical    = "calendar"
  }

  depends_on = [google_project_service.apis]
}

# Each runtime identity reads only its own environment's secrets, as in secrets.tf.
resource "google_secret_manager_secret_iam_member" "runtime_reads_trello" {
  for_each = local.trello_env_secrets

  secret_id = google_secret_manager_secret.trello[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime[each.value.env].member
}

resource "google_secret_manager_secret_iam_member" "runtime_reads_calendar_token" {
  for_each = var.composition_environments

  secret_id = google_secret_manager_secret.calendar_token[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime[each.key].member
}

# Whoever holds the Google token (it is created on their machine) may add new versions of
# it, e.g. after the 7-day re-authorization, without being able to read it back.
resource "google_secret_manager_secret_iam_member" "calendar_token_uploaders" {
  for_each = {
    for pair in setproduct(var.composition_environments, var.calendar_token_uploaders) :
    "${pair[0]}/${pair[1]}" => { env = pair[0], member = pair[1] }
  }

  secret_id = google_secret_manager_secret.calendar_token[each.value.env].secret_id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = each.value.member
}
