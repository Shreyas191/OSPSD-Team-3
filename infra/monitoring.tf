# One symptom-based alert: callers are getting server errors (5xx) from a service.
#
# It uses the request counts Cloud Run records for every response, so it works whatever the
# app reports about itself, and it sees the errors callers see: our own 500/502/503 bodies
# and anything Cloud Run returns itself. Caller mistakes (4xx) are not counted.
#
# It fires on a count, not a percentage: with our traffic (a few requests a minute) one
# failure out of two requests would be a 50% error rate. More than `alert_5xx_threshold`
# server errors from one service within 5 minutes means callers are failing repeatedly, not
# one unlucky request. No traffic means no errors, so silence never fires it.

resource "google_monitoring_notification_channel" "email" {
  display_name = "Cloud storage service alerts (email)"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "server_errors" {
  display_name = "Cloud storage service: callers are getting server errors (5xx)"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "More than ${var.alert_5xx_threshold} 5xx responses in 5 minutes"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "resource.labels.service_name = starts_with(\"${local.service_name}-\")",
        "metric.type = \"run.googleapis.com/request_count\"",
        "metric.labels.response_code_class = \"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.alert_5xx_threshold
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }

      # Delta metrics have no data while there is no traffic; treat that as zero errors.
      evaluation_missing_data = "EVALUATION_MISSING_DATA_INACTIVE"
    }
  }

  alert_strategy {
    # Tell the person both when it starts and when it is over.
    notification_prompts = ["OPENED", "CLOSED"]
    auto_close           = "1800s"
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    mime_type = "text/markdown"
    subject   = "[cloud-storage] 5xx from $${resource.label.service_name}"
    content   = <<-EOT
      **$${resource.label.service_name}** returned more than ${var.alert_5xx_threshold} server errors (5xx) in 5 minutes. Callers are failing.

      - Runbook: https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/docs/runbook.md#alert-callers-are-getting-server-errors-5xx
      - Dashboard (Cloud Run metrics): https://console.cloud.google.com/run/detail/${var.region}/$${resource.label.service_name}/metrics?project=${var.project_id}
      - Logs (errors only): https://console.cloud.google.com/logs/query;query=resource.type%3D%22cloud_run_revision%22%0Aresource.labels.service_name%3D%22$${resource.label.service_name}%22%0AhttpRequest.status%3E%3D500?project=${var.project_id}
    EOT
  }

  depends_on = [google_project_service.apis]
}
