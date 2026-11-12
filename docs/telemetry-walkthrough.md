# Telemetry Walkthrough

This page covers HW2 item 3.3: three requests to the deployed **test** service (a success, an invalid request and a provider failure), and for each one the dashboard point, the log entry and the trace that explain it. It also says how to move between them, so someone who didn't write the instrumentation can explain a request from the telemetry alone.

How the telemetry is produced: [`telemetry.py`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/src/cloud_storage_service/src/cloud_storage_service/telemetry.py) (one JSON log line per request, traces with the Dropbox call as its own span) and [`infra/telemetry.tf`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/infra/telemetry.tf) (metrics). All three requests below ran release `3939708` on Nov 11, 2026.

## How to move from one to the next

| From | To | How |
|---|---|---|
| Dashboard | Log lines | Each panel is per route and outcome. In **Logs Explorer**, filter on the same two fields and the time of the point: `jsonPayload.event="request" AND jsonPayload.route="<route>" AND jsonPayload.outcome="<outcome>"` (add `resource.labels.service_name="cloud-storage-service-test"`). |
| Log line | Trace | Every request line carries `trace_id`, and Cloud Logging links it: in Logs Explorer, open the entry and click **View trace**, or go to `https://console.cloud.google.com/traces/list?project=ospsd-team3&tid=<trace_id>`. |
| Trace | Cause | The `GET …` span is our service; each child `POST https://api.dropboxapi.com/…` span is one Dropbox call. No child span means the request never reached Dropbox. |

Command line, for the same steps:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="cloud-storage-service-test"
  AND jsonPayload.event="request" AND jsonPayload.outcome="provider_error"' --freshness=1h --limit=5
```

## 1. A successful request

`GET /files?path=/` → `200`.

**Dashboard:** a point in the `list_folder` / `ok` series of the request counter and of the latency distribution (`logging.googleapis.com/user/cloud_storage/requests` and `.../request_latency`).

**Log entry** (one line, `INFO`):

```json
{"severity": "INFO", "message": "list_folder 200 ok", "operation": "list_folder", "route": "list_folder",
 "status": 200, "outcome": "ok", "duration_ms": 198, "release": "3939708c533a2ecf7c5c54a85e5eacafa2586f31",
 "trace_id": "737225577288d4d4f40185e7218aa6b7"}
```

**Trace** `737225577288d4d4f40185e7218aa6b7`:

| Span | Duration | Parent | Status |
|---|---|---|---|
| `/files` (Cloud Run's own) | 211.2 ms | — | 200 |
| `GET /files` (our service) | 198.1 ms | `/files` | 200 |
| `POST https://api.dropboxapi.com/2/files/list_folder` | 194.0 ms | `GET /files` | 200 |

**What happened:** the request succeeded, and Dropbox's `list_folder` call took 194 of our 198 ms. If this route got slow, the trace would show whether Dropbox or our code took the time.

## 2. An invalid request

`GET /files/metadata?path=/a/../b` → `400 invalid_request`.

**Dashboard:** a point in the `get_file_metadata` / `invalid_request` series. It is a caller error (4xx), so it is not counted by the 5xx alert.

**Log entry** (`WARNING`):

```json
{"severity": "WARNING", "message": "Request failed: operation='get_file_metadata' status=400 code='invalid_request' provider_failure=False",
 "operation": "get_file_metadata", "route": "get_file_metadata", "status": 400, "outcome": "invalid_request",
 "provider_failure": false, "reason": "query.path: Value error, path must not contain empty, '…' or '…' segments",
 "duration_ms": 1.5, "release": "3939708c533a2ecf7c5c54a85e5eacafa2586f31", "trace_id": "e7095b2c7e174db74d4b8a7efd81b411"}
```

**Trace** `e7095b2c7e174db74d4b8a7efd81b411`:

| Span | Duration | Parent | Status |
|---|---|---|---|
| `GET /files/metadata` (our service) | 1.6 ms | — | 400 |

**What happened:** the path failed validation (a `..` segment) and the service answered in 1.6 ms. There is **no Dropbox span**: the request never reached the provider, so this is the caller's mistake, not ours or Dropbox's. The reason is in the log; the rejected value isn't.

## 3. A provider failure

`GET /files/metadata?path=/drill/report.txt` → `502 provider_error`.

**How it was triggered** (and undone), without touching the revision users reach:

1. A temporary secret `dropbox-refresh-token-drill` with an obviously invalid token was created, readable only by `storage-run-test`.
2. `gcloud run services update cloud-storage-service-test --no-traffic --tag=provider-fail --update-secrets=DROPBOX_REFRESH_TOKEN=dropbox-refresh-token-drill:latest` created revision `00021-sux`, serving **0%** of traffic, reachable only at its `provider-fail---…` URL. Users stayed on `rel-3939708`, whose `/health/provider` kept answering `200`.
3. Two requests to that URL (21:42:27 UTC) returned `502 provider_error`.
4. Restored at once: the template's `DROPBOX_REFRESH_TOKEN` points at `dropbox-refresh-token-test:latest` again, the `provider-fail` tag was removed and the temporary secret deleted. `tofu plan` reported **No changes**, and the smoke check (`scripts/consumer_workflow.py`) passed all 9 steps against the test service.

**Dashboard:** a point in the `get_file_metadata` / `provider_error` series. A 5xx, so it also counts toward the 5xx alert (two errors were below its threshold of 5).

**Log entry** (`ERROR`):

```json
{"severity": "ERROR", "message": "Request failed: operation='get_file_metadata' status=502 code='provider_error' provider_failure=True",
 "operation": "get_file_metadata", "route": "get_file_metadata", "status": 502, "outcome": "provider_error",
 "provider_failure": true, "reason": "Dropbox failed reading '…' (AuthError)", "duration_ms": 97.1,
 "release": "3939708c533a2ecf7c5c54a85e5eacafa2586f31", "trace_id": "efb601a1734b2929409a54b30ffee1f3"}
```

**Trace** `efb601a1734b2929409a54b30ffee1f3`:

| Span | Duration | Parent | Status |
|---|---|---|---|
| `GET /files/metadata` (our service) | 97.2 ms | — | 502 |
| `POST https://api.dropboxapi.com/oauth2/token` | 94.0 ms | `GET /files/metadata` | **400** |

**What happened:** before calling `get_metadata`, the Dropbox SDK tried to exchange the refresh token for an access token, and Dropbox's `oauth2/token` endpoint refused it (400). The service turned that `AuthError` into `502 provider_error` with a fixed message for the caller. `provider_failure: true` in the log and the failing Dropbox span both point at the provider and the credentials, not at our code or the caller. In the [runbook](runbook.md) this is the "Dropbox rejects our credentials" case.

## Checked: no sensitive values

- The log lines above have no token, no header, no file path (quoted text in `reason` is redacted) and no query string.
- Our spans record the URL's path without the query string, and the Dropbox spans only the API endpoint; request bodies are never recorded.
- Cloud Run's own root span (`/files` above) and its request log do record the full URL, query included. They are the platform's records, readable only by project members.
- Metric labels are only `route` and `outcome`, both from fixed sets.

## Teammate walkthrough

A teammate who did not write the instrumentation explains each request using only the dashboard, the logs and the traces above, and records any question they had to ask. Each question is a gap in this page; fix it in the same pull request.

Each dashboard point was followed to its matching log entry and trace. The dashboard showed the operation and outcome; the log gave the status, duration, release and trace ID; and the trace showed whether the request reached Dropbox.

| Request | Explained by | Their explanation | Matches what happened? | Questions they had to ask |
|---|---|---|---|---|
| Success | Victor Wang, Nov 12, 2026 | `GET /files?path=/` returned `200`. The dashboard showed `list_folder` / `ok`. Its log showed a 198 ms request, and the trace showed Dropbox's `list_folder` call succeeding in 194 ms. Most of the request time was spent waiting on Dropbox. | Yes | None |
| Invalid request | Victor Wang, Nov 12, 2026 | `GET /files/metadata?path=/a/../b` returned `400 invalid_request`. The dashboard showed `get_file_metadata` / `invalid_request`. The log said the path failed validation and marked it as not a provider failure. The trace showed the service rejecting it in about 1.6 ms, with no Dropbox span. So the request was rejected before it reached Dropbox. | Yes | None |
| Provider failure | Victor Wang, Nov 12, 2026 | `GET /files/metadata?path=/drill/report.txt` returned `502 provider_error`. The dashboard showed `get_file_metadata` / `provider_error`, and the log marked `provider_failure: true`. The trace showed Dropbox's token endpoint returning `400` and taking about 94 ms of the 97 ms request. That points to Dropbox rejecting the credentials. | Yes | None |

**Conclusion:** the telemetry distinguished a successful provider call, a caller error rejected before reaching the provider, and a provider-side credential failure.
