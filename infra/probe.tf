# External synthetic probe (HW2 optional extension): a request made from outside our
# service, on a schedule, through the public URL a consumer uses. See docs/synthetic-probe.md.
#
# Every 5 minutes, Cloud Monitoring's uptime checkers call
#   GET /files/metadata?path=/synthetic-probe/absent
# on each environment. The path never exists, so a healthy service answers
# 404 {"error": {"code": "not_found", ...}}. It is the operation the SLO covers
# (docs/slo.md), it is read-only, it needs no file to be kept in Dropbox, and it still makes
# the Dropbox call (files_get_metadata), so a Dropbox or credential failure fails it.
#
# A check passes only on status 404 AND that exact error code in the body: a page that
# answers 200 to everything, or a 404 from a route that doesn't exist, is a failure.

locals {
  probe_path = "/files/metadata?path=/synthetic-probe/absent"
}

resource "google_monitoring_uptime_check_config" "probe" {
  for_each = var.environments

  display_name = "Synthetic probe: ${local.service_name}-${each.key} metadata"
  period       = "300s"
  timeout      = "10s"

  # Three checkers outside the service's region (us-central1 is Iowa), so the request
  # crosses the public internet to reach Google's front end, as a consumer's would.
  selected_regions = ["USA_OREGON", "USA_VIRGINIA", "EUROPE"]

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = trimprefix(google_cloud_run_v2_service.service[each.key].uri, "https://")
    }
  }

  http_check {
    path           = local.probe_path
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"
    accepted_response_status_codes {
      status_value = 404
    }
  }

  content_matchers {
    content = "\"not_found\""
    matcher = "MATCHES_JSON_PATH"
    json_path_matcher {
      json_path    = "$.error.code"
      json_matcher = "EXACT_MATCH"
    }
  }

  user_labels = {
    environment = each.key
  }

  depends_on = [google_project_service.apis]
}
