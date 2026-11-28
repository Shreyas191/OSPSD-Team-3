# HW3 Milestone 2 (#112, failure analysis item 4): what happened to students' approved
# changes, on the golden-signals dashboard, and an alert when a change may or may not have
# happened.
#
# `POST /proposals/{id}/execute` answers 200 even when a change failed or is uncertain, so the
# request metrics in telemetry.tf and the 5xx alert can't see execution outcomes. Instead, the
# API writes one JSON line per run (cloud_storage_service/execution.py, `_log_execution`):
#
#   {"event": "proposal_execution", "final_status": "done" | "failed", "changes": 2,
#    "changes_completed": 1, "changes_failed": 0, "changes_uncertain": 1, "proposal_id": ...}
#
# The counts are of the proposal's outcomes after the run, so a retry of a proposal that still
# has an uncertain change reports it again. The only label is the final status (two values);
# proposal IDs are never labels.

locals {
  execution_lines = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=~\"^${local.service_name}-\"",
    "jsonPayload.event=\"proposal_execution\"",
  ])
}

# Runs, by how they ended: `done` (every change completed) or `failed` (some change failed or
# is uncertain; completed ones stay).
resource "google_logging_metric" "proposal_executions" {
  name        = "cloud_storage/proposal_executions"
  description = "Runs of approved proposals, by final status (done or failed)."
  filter      = local.execution_lines

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "final_status"
      value_type  = "STRING"
      description = "done, or failed when a change failed or is uncertain"
    }
  }
  label_extractors = {
    final_status = "EXTRACT(jsonPayload.final_status)"
  }
}

# Runs that left at least one change uncertain: it may exist in the student's calendar or board
# without being recorded, so it is never retried and needs the student to check (P3).
resource "google_logging_metric" "proposal_executions_uncertain" {
  name        = "cloud_storage/proposal_executions_uncertain"
  description = "Runs of approved proposals that left at least one change uncertain."
  filter      = "${local.execution_lines} AND jsonPayload.changes_uncertain > 0"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "execution_uncertain" {
  display_name = "Course Autopilot: a student's change may or may not have happened (uncertain)"
  combiner     = "OR"
  severity     = "WARNING"

  conditions {
    display_name = "A run of an approved proposal left a change uncertain"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.proposal_executions_uncertain.name}\"",
      ])
      # One is enough: each uncertain change is a student who has to check their account.
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }

      # No runs means nothing uncertain.
      evaluation_missing_data = "EVALUATION_MISSING_DATA_INACTIVE"
    }
  }

  alert_strategy {
    notification_prompts = ["OPENED", "CLOSED"]
    auto_close           = "1800s"
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    mime_type = "text/markdown"
    subject   = "[course-autopilot] uncertain change on $${resource.label.service_name}"
    content   = <<-EOT
      A run of a student's approved proposal on **$${resource.label.service_name}** left a change **uncertain**: the provider may have made it, but we couldn't confirm it (a lost reply, a read-back that differed, or a run that stopped mid-write). It is never retried automatically, so it can't be made twice; the student has to check and settle it.

      - Runbook: https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/docs/runbook.md#alert-a-change-may-or-may-not-have-happened
      - Dashboard (API, execution outcomes): ${local.dashboard_url}
      - The runs (logs): https://console.cloud.google.com/logs/query;query=jsonPayload.event%3D%22proposal_execution%22%0AjsonPayload.changes_uncertain%3E0?project=${var.project_id}
    EOT
  }

  depends_on = [google_project_service.apis]
}

locals {
  executions_metric           = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.proposal_executions.name}\" AND ${local.run_revision}"
  executions_uncertain_metric = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.proposal_executions_uncertain.name}\" AND ${local.run_revision}"

  # Two tiles for the bottom row of the golden-signals dashboard (dashboard.tf).
  execution_tiles = [
    {
      yPos = 64, width = 24, height = 16
      widget = {
        title = "Execution outcomes: runs of approved proposals per minute, by final status, and runs that left a change uncertain"
        xyChart = {
          chartOptions = { mode = "COLOR" }
          yAxis        = { label = "runs per minute", scale = "LINEAR" }
          dataSets = [
            {
              plotType           = "STACKED_BAR"
              legendTemplate     = "$${metric.labels.final_status}"
              minAlignmentPeriod = "60s"
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = local.executions_metric
                  aggregation = {
                    alignmentPeriod    = "60s"
                    perSeriesAligner   = "ALIGN_DELTA"
                    crossSeriesReducer = "REDUCE_SUM"
                    groupByFields      = ["metric.label.final_status"]
                  }
                }
              }
            },
            {
              plotType           = "LINE"
              legendTemplate     = "left a change uncertain (alert)"
              minAlignmentPeriod = "60s"
              timeSeriesQuery = {
                timeSeriesFilter = {
                  filter = local.executions_uncertain_metric
                  aggregation = {
                    alignmentPeriod    = "60s"
                    perSeriesAligner   = "ALIGN_DELTA"
                    crossSeriesReducer = "REDUCE_SUM"
                    groupByFields      = ["resource.label.service_name"]
                  }
                }
              }
            },
          ]
        }
      }
    },
    {
      xPos = 24, yPos = 64, width = 24, height = 16
      widget = {
        title = "Runs that didn't finish: their log lines (proposal ID and counts; never the changes' content)"
        logsPanel = {
          filter        = "${local.execution_lines} AND NOT jsonPayload.final_status=\"done\""
          resourceNames = ["projects/${var.project_id}"]
        }
      }
    },
  ]
}
