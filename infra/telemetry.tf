# Metrics and traces for the service (item 3.1). The service writes one JSON log line per
# request (src/cloud_storage_service/.../telemetry.py); these metrics count and time those
# lines, so they need no metrics SDK in the app. Traces go to Cloud Trace.
#
# Labels are bounded on purpose: `route` is the route's name (about 15 values, or
# "unmatched") and `outcome` is "ok" or one error code (about 12 values). Paths, IDs and
# URLs are never labels. The service name comes from the resource, not a label.

locals {
  request_lines = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=starts_with(\"${local.service_name}-\")",
    "jsonPayload.event=\"request\"",
  ])
}

# Traffic, and errors by category: requests per route and outcome.
resource "google_logging_metric" "requests" {
  name        = "cloud_storage/requests"
  description = "Requests to the cloud storage service, by route and outcome (ok or the error code)."
  filter      = local.request_lines

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "route"
      value_type  = "STRING"
      description = "Route name, e.g. get_file_metadata"
    }
    labels {
      key         = "outcome"
      value_type  = "STRING"
      description = "ok, or the error code (not_found, provider_error, service_busy, ...)"
    }
  }
  label_extractors = {
    route   = "EXTRACT(jsonPayload.route)"
    outcome = "EXTRACT(jsonPayload.outcome)"
  }
}

# Latency per route, kept per outcome so successful and failed requests are measured apart.
resource "google_logging_metric" "request_latency" {
  name        = "cloud_storage/request_latency"
  description = "Time the service spent on each request, in milliseconds, by route and outcome."
  filter      = local.request_lines

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "ms"
    labels {
      key        = "route"
      value_type = "STRING"
    }
    labels {
      key        = "outcome"
      value_type = "STRING"
    }
  }
  value_extractor = "EXTRACT(jsonPayload.duration_ms)"
  label_extractors = {
    route   = "EXTRACT(jsonPayload.route)"
    outcome = "EXTRACT(jsonPayload.outcome)"
  }
  bucket_options {
    # 1 ms to about 60 s in 40 steps.
    exponential_buckets {
      num_finite_buckets = 40
      growth_factor      = 1.32
      scale              = 1
    }
  }
}

# Each runtime identity may write traces, and nothing else in Cloud Trace.
resource "google_project_iam_member" "runtime_writes_traces" {
  for_each = var.environments

  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = google_service_account.runtime[each.key].member
}
