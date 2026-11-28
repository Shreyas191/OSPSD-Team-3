# HW3 Milestone 2 (#112): task success, latency, active work and model usage for the agent
# service, and an alert for a failure students see.
#
# The agent writes one JSON line per run (course_agent_service/app.py, `"event": "agent_run"`)
# with the stop reason, the duration and the model usage. These log-based metrics count and
# measure those lines, as telemetry.tf does for the API's request lines. The only label is the
# stop reason (about 11 values); conversation IDs are never labels.
#
# Why a separate alert: a turn that fails because Gemini or the API is unreachable still
# answers 200 with `stop_reason`, so the 5xx alert in monitoring.tf never sees it.

locals {
  agent_service_prefix = "course-agent-"

  agent_run_lines = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=~\"^${local.agent_service_prefix}\"",
    "jsonPayload.event=\"agent_run\"",
  ])

  # Stop reasons where the student got no answer because something we depend on failed.
  # Kept apart from limits and invalid proposals, which are the agent's own guards working.
  agent_failure_reasons = ["model_unavailable", "api_unavailable"]
}

# Turns, by how they ended: task success and its failures.
resource "google_logging_metric" "agent_runs" {
  name        = "course_agent/runs"
  description = "Agent turns, by stop reason (replied, proposal_ready, model_unavailable, ...)."
  filter      = local.agent_run_lines

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "stop_reason"
      value_type  = "STRING"
      description = "How the turn ended (course_agent_service/loop.py)"
    }
  }
  label_extractors = {
    stop_reason = "EXTRACT(jsonPayload.stop_reason)"
  }
}

# How long a whole turn took, from the student's message to the reply.
resource "google_logging_metric" "agent_run_duration" {
  name        = "course_agent/run_duration"
  description = "Duration of each agent turn in milliseconds, by stop reason."
  filter      = local.agent_run_lines

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "ms"
    labels {
      key        = "stop_reason"
      value_type = "STRING"
    }
  }
  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  label_extractors = {
    stop_reason = "EXTRACT(jsonPayload.stop_reason)"
  }
  bucket_options {
    # 1 ms to about 66 s in 40 steps; a turn stops at 60 s.
    exponential_buckets {
      num_finite_buckets = 40
      growth_factor      = 1.32
      scale              = 1
    }
  }
}

# Model usage per turn: calls (the free tier's binding limit is calls per minute) and tokens
# (the cost). One metric per field, because a log-based metric takes one value per line.
resource "google_logging_metric" "agent_run_usage" {
  for_each = {
    model_calls   = { unit = "1", scale = 1, growth = 1.5, buckets = 12, about = "Model calls" }
    input_tokens  = { unit = "1", scale = 100, growth = 1.5, buckets = 20, about = "Input tokens" }
    output_tokens = { unit = "1", scale = 10, growth = 1.5, buckets = 20, about = "Output tokens" }
  }

  name        = "course_agent/run_${each.key}"
  description = "${each.value.about} per agent turn."
  filter      = local.agent_run_lines

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = each.value.unit
  }
  value_extractor = "EXTRACT(jsonPayload.${each.key})"
  bucket_options {
    exponential_buckets {
      num_finite_buckets = each.value.buckets
      growth_factor      = each.value.growth
      scale              = each.value.scale
    }
  }
}

resource "google_monitoring_alert_policy" "agent_turns_failing" {
  display_name = "Course Autopilot: students' turns are failing (model or API unavailable)"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "More than ${var.alert_agent_failures_threshold} turns ended by an unavailable model or API in 5 minutes"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "metric.type = \"logging.googleapis.com/user/${google_logging_metric.agent_runs.name}\"",
        "metric.labels.stop_reason = one_of(${join(", ", [for r in local.agent_failure_reasons : "\"${r}\""])})",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.alert_agent_failures_threshold
      duration        = "60s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }

      # No turns means no failures.
      evaluation_missing_data = "EVALUATION_MISSING_DATA_INACTIVE"
    }
  }

  # The agent service's own server errors (a crash, or Cloud Run refusing requests), which
  # the 5xx alert in monitoring.tf doesn't cover: it watches the API services only.
  conditions {
    display_name = "More than ${var.alert_5xx_threshold} 5xx responses from the agent service in 5 minutes"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "resource.labels.service_name = starts_with(\"${local.agent_service_prefix}\")",
        "metric.type = \"run.googleapis.com/request_count\"",
        "metric.labels.response_code_class = \"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.alert_5xx_threshold
      duration        = "60s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.labels.service_name"]
      }

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
    subject   = "[course-autopilot] turns failing on $${resource.label.service_name}"
    content   = <<-EOT
      Students' turns on **$${resource.label.service_name}** are ending without an answer: the model (Gemini) or the API service was unavailable, or the agent itself returned server errors.

      - Runbook: https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/docs/runbook.md#alert-students-turns-are-failing
      - Dashboard (agent): ${local.agent_dashboard_url}
      - Failed turns (logs): https://console.cloud.google.com/logs/query;query=jsonPayload.event%3D%22agent_run%22%0AjsonPayload.stop_reason%3D%28%22model_unavailable%22%20OR%20%22api_unavailable%22%29?project=${var.project_id}
    EOT
  }

  depends_on = [google_project_service.apis]
}

locals {
  agent_runs_metric     = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.agent_runs.name}\" AND ${local.run_revision}"
  agent_duration_metric = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.agent_run_duration.name}\" AND ${local.run_revision}"
  agent_usage_metric    = { for k, m in google_logging_metric.agent_run_usage : k => "metric.type=\"logging.googleapis.com/user/${m.name}\" AND ${local.run_revision}" }

  agent_dashboard = {
    displayName = "Course Autopilot agent: task success"
    dashboardFilters = [{
      filterType  = "RESOURCE_LABEL"
      labelKey    = "service_name"
      stringValue = "${local.agent_service_prefix}test"
    }]
    mosaicLayout = {
      columns = 48
      tiles = [
        {
          width = 16, height = 16
          widget = {
            title = "How to read this dashboard"
            text = {
              format  = "MARKDOWN"
              content = <<-EOT
                **Service:** `${local.agent_service_prefix}test` (default) or `-demo` in the filter bar. One agent turn is one student message and the work it caused.

                **Task success** is a turn that ends `replied` or `proposal_ready`. `model_unavailable` and `api_unavailable` mean the student got no answer because Gemini or our API failed: the alert watches those. `limit_*` and `invalid_proposal` are the agent's guards stopping a turn.

                **Not shown here:** what happened to each approved change. That's the execution-outcomes row of the API's golden-signals dashboard (${local.dashboard_url}), with its own alert for uncertain changes. Failures and recovery: [failure analysis](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/docs/failure-analysis.md). Incidents: [runbook](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/docs/runbook.md#alert-students-turns-are-failing).
              EOT
            }
          }
        },
        {
          xPos = 16, width = 32, height = 16
          widget = {
            title = "Task success: turns per minute, by how they ended"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "turns per minute", scale = "LINEAR" }
              dataSets = [{
                plotType           = "STACKED_BAR"
                legendTemplate     = "$${metric.labels.stop_reason}"
                minAlignmentPeriod = "60s"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = local.agent_runs_metric
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.stop_reason"]
                    }
                  }
                }
              }]
            }
          }
        },
        {
          yPos = 16, width = 24, height = 16
          widget = {
            title = "Errors: turns that failed (model or API unavailable) and agent 5xx, per minute"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "per minute", scale = "LINEAR" }
              dataSets = [
                {
                  plotType           = "STACKED_BAR"
                  legendTemplate     = "turn: $${metric.labels.stop_reason}"
                  minAlignmentPeriod = "60s"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "${local.agent_runs_metric} AND metric.labels.stop_reason = one_of(${join(", ", [for r in local.agent_failure_reasons : "\"${r}\""])})"
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_SUM"
                        groupByFields      = ["metric.label.stop_reason"]
                      }
                    }
                  }
                },
                {
                  plotType           = "STACKED_BAR"
                  legendTemplate     = "agent HTTP $${metric.labels.response_code}"
                  minAlignmentPeriod = "60s"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"run.googleapis.com/request_count\" AND ${local.run_revision} AND metric.labels.response_code_class = \"5xx\""
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_SUM"
                        groupByFields      = ["metric.label.response_code"]
                      }
                    }
                  }
                },
              ]
            }
          }
        },
        {
          xPos = 24, yPos = 16, width = 24, height = 16
          widget = {
            title = "Latency: full turn (p50 and p95), against the 45 s objective and the 60 s limit"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "milliseconds", scale = "LINEAR" }
              thresholds = [
                { label = "SLO: p95 full turn 45 s (readiness record)", value = 45000 },
                { label = "Limit: a turn stops at 60 s", value = 60000 },
              ]
              dataSets = [
                for p in ["50", "95"] : {
                  plotType           = "LINE"
                  legendTemplate     = "p${p}"
                  minAlignmentPeriod = "60s"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = local.agent_duration_metric
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_PERCENTILE_${p}"
                        groupByFields      = ["resource.label.service_name"]
                      }
                    }
                  }
                }
              ]
            }
          }
        },
        {
          yPos = 32, width = 24, height = 16
          widget = {
            title = "Active work: concurrent turns per agent instance, and instances"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "concurrent requests (peak per minute, mean of instances)", scale = "LINEAR" }
              y2Axis       = { label = "instances", scale = "LINEAR" }
              dataSets = [
                {
                  plotType           = "LINE"
                  legendTemplate     = "concurrent: $${resource.labels.revision_name}"
                  minAlignmentPeriod = "60s"
                  targetAxis         = "Y1"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"run.googleapis.com/container/max_request_concurrencies\" AND ${local.run_revision} AND metric.labels.state = \"active\""
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_MEAN"
                        groupByFields      = ["resource.label.revision_name"]
                      }
                    }
                  }
                },
                {
                  plotType           = "STACKED_BAR"
                  legendTemplate     = "instances ($${metric.labels.state})"
                  minAlignmentPeriod = "60s"
                  targetAxis         = "Y2"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"run.googleapis.com/container/instance_count\" AND ${local.run_revision}"
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_MAX"
                        crossSeriesReducer = "REDUCE_SUM"
                        groupByFields      = ["metric.label.state"]
                      }
                    }
                  }
                },
              ]
            }
          }
        },
        {
          xPos = 24, yPos = 32, width = 24, height = 16
          widget = {
            title = "Model usage per turn: model calls and tokens (p50 and p95)"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "tokens per turn", scale = "LINEAR" }
              y2Axis       = { label = "model calls per turn", scale = "LINEAR" }
              dataSets = concat(
                [
                  for pair in setproduct(["input_tokens", "output_tokens"], ["50", "95"]) : {
                    plotType           = "LINE"
                    legendTemplate     = "${pair[0]} p${pair[1]}"
                    minAlignmentPeriod = "60s"
                    targetAxis         = "Y1"
                    timeSeriesQuery = {
                      timeSeriesFilter = {
                        filter = local.agent_usage_metric[pair[0]]
                        aggregation = {
                          alignmentPeriod    = "60s"
                          perSeriesAligner   = "ALIGN_DELTA"
                          crossSeriesReducer = "REDUCE_PERCENTILE_${pair[1]}"
                          groupByFields      = ["resource.label.service_name"]
                        }
                      }
                    }
                  }
                ],
                [{
                  plotType           = "LINE"
                  legendTemplate     = "model calls p95"
                  minAlignmentPeriod = "60s"
                  targetAxis         = "Y2"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = local.agent_usage_metric["model_calls"]
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_PERCENTILE_95"
                        groupByFields      = ["resource.label.service_name"]
                      }
                    }
                  }
                }],
              )
            }
          }
        },
        {
          yPos = 48, width = 48, height = 16
          widget = {
            title = "Turns that didn't succeed: their run records (tool outcomes, timing, tokens; never message text)"
            logsPanel = {
              filter        = "${local.run_revision} AND resource.labels.service_name=~\"^${local.agent_service_prefix}\" AND jsonPayload.event=\"agent_run\" AND NOT jsonPayload.stop_reason=(\"replied\" OR \"proposal_ready\")"
              resourceNames = ["projects/${var.project_id}"]
            }
          }
        },
      ]
    }
  }
}

resource "google_monitoring_dashboard" "agent" {
  dashboard_json = jsonencode(local.agent_dashboard)

  depends_on = [google_project_service.apis]
}

locals {
  agent_dashboard_url = "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.agent.id), 3)}?project=${var.project_id}"
}

output "agent_dashboard_url" {
  description = "The agent's task-success dashboard in the Cloud console."
  value       = local.agent_dashboard_url
}
