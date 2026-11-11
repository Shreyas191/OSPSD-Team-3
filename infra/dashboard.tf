# The four golden signals for each shared operation, with the release that served the
# requests (item 3.2, docs/dashboard.md). One Cloud Monitoring dashboard for both
# environments; the filter bar picks the service (test by default:
# hw2 deploys there, so it has the traffic).
#
# It reads the two log-based metrics in telemetry.tf, one more below for the release, and
# Cloud Run's own concurrency metric for saturation. Every chart groups by a bounded label:
# the route's name, the outcome (ok or an error code), the release, the revision or the
# service.

# Which release served requests: one count per release. A separate metric, so the release
# doesn't multiply the route x outcome series of the other two. It has one value per
# deployed commit, and each revision is already its own series, so it adds no new series.
resource "google_logging_metric" "requests_by_release" {
  name        = "cloud_storage/requests_by_release"
  description = "Requests to the cloud storage service, by the release (commit SHA) that served them."
  filter      = local.request_lines

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "release"
      value_type  = "STRING"
      description = "Full commit SHA of the image that served the request"
    }
  }
  label_extractors = {
    release = "EXTRACT(jsonPayload.release)"
  }
}

locals {
  run_revision = "resource.type=\"cloud_run_revision\""

  requests_metric = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.requests.name}\" AND ${local.run_revision}"
  latency_metric  = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.request_latency.name}\" AND ${local.run_revision}"
  release_metric  = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.requests_by_release.name}\" AND ${local.run_revision}"

  # Health probes (Cloud Run's liveness probe calls /health every 30 s) are not callers'
  # requests, so the traffic, error and latency charts leave them out.
  callers = "metric.labels.route != \"check_service_health\" AND metric.labels.route != \"check_provider_health\""

  per_route = ["metric.label.route"]

  dashboard = {
    displayName = "Cloud storage service: golden signals"
    dashboardFilters = [{
      filterType  = "RESOURCE_LABEL"
      labelKey    = "service_name"
      stringValue = "${local.service_name}-test"
    }]
    mosaicLayout = {
      columns = 48
      # A position of 0 is left out (a tile with no xPos is at the left edge): the API drops
      # zero values, and the provider would then show the dashboard as changed on every plan.
      tiles = [
        {
          width = 20, height = 16
          widget = {
            title = "How to read this dashboard"
            text = {
              format  = "MARKDOWN"
              content = <<-EOT
                **Service:** pick `${local.service_name}-test` (the default, where `hw2` deploys) or `-demo` in the filter bar. Each chart shows one line per shared operation (route name). Times are per minute.

                **Release:** the commit that served the requests, in the two release panels. Open it at `https://github.com/Shreyas191/OSPSD-Team-3/commit/<sha>`.

                **No data is not health.** The services scale to zero, so with no requests every chart is empty. Check `GET /health/provider` or run the smoke check (`scripts/consumer_workflow.py`).

                **Error codes:** `invalid_request` 400 · `permission_denied` 403 · `not_found` 404 · `conflict` 409 · `file_too_large` 413 · `invalid_operation` 422 · `internal_error` 500 · `provider_error` 502 · `provider_unavailable` 503 · `service_busy` 503 (refused: the service is full). `unmatched` is a request that matched no route, or one refused before routing.

                **From a failed request to its log and trace:** the "Failed requests" log panel below, or [Logs Explorer](https://console.cloud.google.com/logs/query;query=jsonPayload.event%3D%22request%22%20severity%3E%3DWARNING?project=${var.project_id}). Each entry links to its trace (health checks aren't traced). What to do about each panel: [docs/dashboard.md](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/docs/dashboard.md). Incidents: [runbook](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/docs/runbook.md).
              EOT
            }
          }
        },
        {
          xPos = 20, width = 12, height = 16
          widget = {
            title = "Release serving requests (latest minute with traffic)"
            timeSeriesTable = {
              metricVisualization = "NUMBER"
              dataSets = [{
                tableTemplate = "$${metric.labels.release}"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = local.release_metric
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.release"]
                    }
                  }
                }
              }]
            }
          }
        },
        {
          xPos = 32, width = 16, height = 16
          widget = {
            title = "Requests by release (a rollout or rollback shows as a change of colour)"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "requests per minute", scale = "LINEAR" }
              dataSets = [{
                plotType           = "STACKED_BAR"
                legendTemplate     = "$${metric.labels.release}"
                minAlignmentPeriod = "60s"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = local.release_metric
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.release"]
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
            title = "Traffic: requests per minute, by operation"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "requests per minute", scale = "LINEAR" }
              dataSets = [{
                plotType           = "STACKED_BAR"
                legendTemplate     = "$${metric.labels.route}"
                minAlignmentPeriod = "60s"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "${local.requests_metric} AND ${local.callers}"
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = local.per_route
                    }
                  }
                }
              }]
            }
          }
        },
        {
          xPos = 24, yPos = 16, width = 24, height = 16
          widget = {
            title = "Errors: failed requests per minute, by operation and error code"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "failed requests per minute", scale = "LINEAR" }
              dataSets = [{
                plotType           = "STACKED_BAR"
                legendTemplate     = "$${metric.labels.route} · $${metric.labels.outcome}"
                minAlignmentPeriod = "60s"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "${local.requests_metric} AND ${local.callers} AND metric.labels.outcome != \"ok\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.route", "metric.label.outcome"]
                    }
                  }
                }
              }]
            }
          }
        },
        {
          yPos = 32, width = 24, height = 16
          widget = {
            title = "Latency of successful requests (p50 and p95), by operation"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "milliseconds", scale = "LINEAR" }
              dataSets = [
                for p in ["50", "95"] : {
                  plotType           = "LINE"
                  legendTemplate     = "p${p} $${metric.labels.route}"
                  minAlignmentPeriod = "60s"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "${local.latency_metric} AND ${local.callers} AND metric.labels.outcome = \"ok\""
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_PERCENTILE_${p}"
                        groupByFields      = local.per_route
                      }
                    }
                  }
                }
              ]
            }
          }
        },
        {
          xPos = 24, yPos = 32, width = 24, height = 16
          widget = {
            title = "Latency of failed requests (p50 and p95), by operation"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "milliseconds", scale = "LINEAR" }
              dataSets = [
                for p in ["50", "95"] : {
                  plotType           = "LINE"
                  legendTemplate     = "p${p} $${metric.labels.route}"
                  minAlignmentPeriod = "60s"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "${local.latency_metric} AND ${local.callers} AND metric.labels.outcome != \"ok\""
                      aggregation = {
                        alignmentPeriod    = "60s"
                        perSeriesAligner   = "ALIGN_DELTA"
                        crossSeriesReducer = "REDUCE_PERCENTILE_${p}"
                        groupByFields      = local.per_route
                      }
                    }
                  }
                }
              ]
            }
          }
        },
        {
          yPos = 48, width = 24, height = 16
          widget = {
            title = "Saturation: concurrent requests per instance, by revision, against the limit of ${var.max_in_flight_requests}"
            xyChart = {
              chartOptions = { mode = "COLOR" }
              yAxis        = { label = "concurrent requests (each instance's peak in the minute, averaged)", scale = "LINEAR" }
              y2Axis       = { label = "requests refused per minute", scale = "LINEAR" }
              thresholds = [{
                label = "MAX_IN_FLIGHT_REQUESTS: beyond this the service answers 503 service_busy"
                value = var.max_in_flight_requests
              }]
              dataSets = [
                {
                  plotType           = "LINE"
                  legendTemplate     = "$${resource.labels.revision_name}"
                  minAlignmentPeriod = "60s"
                  targetAxis         = "Y1"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"run.googleapis.com/container/max_request_concurrencies\" AND ${local.run_revision} AND metric.labels.state = \"active\""
                      # Cloud Run reports each instance's peak per minute in buckets 2 wide above
                      # 10, so a percentile reads up to 2 high (10 would show as 11.98). The mean
                      # is exact. Per revision, so a candidate idle during a deploy doesn't lower it.
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
                  legendTemplate     = "refused: 503 service_busy"
                  minAlignmentPeriod = "60s"
                  targetAxis         = "Y2"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "${local.requests_metric} AND metric.labels.outcome = \"service_busy\""
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
          xPos = 24, yPos = 48, width = 24, height = 16
          widget = {
            title = "Failed requests: their log lines (each request links to its trace)"
            logsPanel = {
              filter        = "${local.run_revision} AND resource.labels.service_name=~\"^${local.service_name}-\" AND jsonPayload.event=\"request\" AND severity>=WARNING"
              resourceNames = ["projects/${var.project_id}"]
            }
          }
        },
      ]
    }
  }
}

resource "google_monitoring_dashboard" "golden_signals" {
  dashboard_json = jsonencode(local.dashboard)

  depends_on = [google_project_service.apis]
}

locals {
  # The dashboard's id is projects/<number>/dashboards/<id>; the console wants the last part.
  dashboard_url = "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.golden_signals.id), 3)}?project=${var.project_id}"
}

output "dashboard_url" {
  description = "The golden-signals dashboard in the Cloud console."
  value       = local.dashboard_url
}
