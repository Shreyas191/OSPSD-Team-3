"""Report the external synthetic probe as its own SLI, next to what the server logged for it.

    uv run python scripts/probe_report.py --project <project-id> \
        --service cloud-storage-service-demo --days 7
    uv run python scripts/probe_report.py --project <project-id> \
        --service cloud-storage-service-test \
        --start 2026-11-11T22:40:00Z --end 2026-11-11T23:40:00Z

Two sources, for the same requests (docs/synthetic-probe.md):

- **The probe's view:** every execution of the uptime check (``infra/probe.tf``), from
  Cloud Monitoring's ``uptime_check/check_passed`` and ``uptime_check/request_latency``.
  An execution passed if it got ``404`` with ``error.code == "not_found"`` within 10 s.
- **The server's view:** Cloud Run's request log (``run.googleapis.com/requests``), the
  source of the server-side SLI (docs/slo.md), filtered to the probe's own requests.

It prints counts only. Exit status: 0 when the probe SLI meets its target, 1 when it
doesn't, 2 when the window has no probe executions (no data is not a pass).
"""

# ruff: noqa: T201, INP001, S603, S607 - a command-line script; it runs gcloud with fixed arguments

import argparse
import collections
import datetime
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

PROBE_PATH = "/synthetic-probe/absent"
AVAILABILITY_TARGET = 0.99
LATENCY_THRESHOLD_MS = 1000
LATENCY_TARGET = 0.95
MONITORING = "https://monitoring.googleapis.com/v3/projects"
UPTIME = "monitoring.googleapis.com/uptime_check"
# The check runs every 5 minutes (infra/probe.tf); a little less, to allow for jitter.
PERIOD = datetime.timedelta(seconds=290)


@dataclass(frozen=True)
class Execution:
    """One run of the uptime check from one checker location."""

    time: str
    location: str
    passed: bool
    latency_ms: float | None


def _token() -> str:
    # Used only in the Authorization header below; never printed.
    return subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _get(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https host
        body: dict[str, Any] = json.load(response)
    return body


def find_check(project: str, service: str, token: str) -> str:
    """Return the uptime check ID that probes ``service``."""
    for check in _get(f"{MONITORING}/{project}/uptimeCheckConfigs", token).get("uptimeCheckConfigs", []):
        host = check.get("monitoredResource", {}).get("labels", {}).get("host", "")
        if host.startswith(f"{service}-"):
            return str(check["name"]).rsplit("/", 1)[-1]
    sys.exit(f"No uptime check probes {service}; apply infra/probe.tf first.")


def _series(project: str, token: str, metric: str, check_id: str, window: tuple[str, str]) -> list[dict[str, Any]]:
    start, end = window
    query = urllib.parse.urlencode(
        {
            "filter": f'metric.type="{metric}" AND metric.labels.check_id="{check_id}"',
            "interval.startTime": start,
            "interval.endTime": end,
            "pageSize": 100000,
        },
    )
    series: list[dict[str, Any]] = []
    url = f"{MONITORING}/{project}/timeSeries?{query}"
    while True:
        page = _get(url, token)
        series.extend(page.get("timeSeries", []))
        if not page.get("nextPageToken"):
            return series
        url = f"{MONITORING}/{project}/timeSeries?{query}&pageToken={page['nextPageToken']}"


def _time(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def read_executions(project: str, check_id: str, start: str, end: str) -> list[Execution]:
    """Return every execution of the check in the window, with its result and latency.

    Cloud Monitoring repeats each execution's result every 10 s until the next one, so the
    points are grouped back into executions: a new one starts when the result or the latency
    changes, or a whole period has passed (two identical results in a row). Points are read
    from one period before ``start``, so an execution made before the window, and still
    repeated inside it, is recognised and left out.
    """
    token = _token()
    early = (_time(start) - PERIOD).strftime("%Y-%m-%dT%H:%M:%SZ")
    passed = _series(project, token, f"{UPTIME}/check_passed", check_id, (early, end))
    latency = _series(project, token, f"{UPTIME}/request_latency", check_id, (early, end))
    latencies = {
        (s["metric"]["labels"]["checker_location"], p["interval"]["endTime"]): p["value"]["doubleValue"]
        for s in latency
        for p in s["points"]
    }
    executions: list[Execution] = []
    for series in passed:
        location = series["metric"]["labels"]["checker_location"]
        current: Execution | None = None
        for point in sorted(series["points"], key=lambda p: p["interval"]["endTime"]):
            time = point["interval"]["endTime"]
            sample = Execution(time, location, point["value"]["boolValue"], latencies.get((location, time)))
            same = current is not None and (current.passed, current.latency_ms) == (sample.passed, sample.latency_ms)
            if not same or current is None or _time(time) - _time(current.time) >= PERIOD:
                current = sample
                executions.append(sample)
    return [e for e in executions if _time(e.time) >= _time(start)]


def read_server_view(project: str, service: str, start: str, end: str) -> collections.Counter[int]:
    """Count, by status, the probe's requests that Cloud Run logged."""
    log_filter = " AND ".join(
        [
            f'logName="projects/{project}/logs/run.googleapis.com%2Frequests"',
            'resource.type="cloud_run_revision"',
            f'resource.labels.service_name="{service}"',
            'httpRequest.userAgent:"GoogleStackdriverMonitoring-UptimeChecks"',
            f'httpRequest.requestUrl:"{PROBE_PATH}"',
            f'timestamp>="{start}"',
            f'timestamp<"{end}"',
        ],
    )
    output = subprocess.run(
        ["gcloud", "logging", "read", log_filter, f"--project={project}", "--format=json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return collections.Counter(int(e.get("httpRequest", {}).get("status", 0)) for e in json.loads(output or "[]"))


def report(executions: list[Execution], server: collections.Counter[int]) -> int:
    """Print the probe SLI, the server's view of the same requests, and return the exit status."""
    if not executions:
        print("NO DATA: the probe did not run in this window. This is not a pass.")
        return 2
    good = [e for e in executions if e.passed]
    fast = [e for e in good if e.latency_ms is not None and e.latency_ms <= LATENCY_THRESHOLD_MS]
    by_location = collections.Counter((e.location, e.passed) for e in executions)

    print(f"Probe executions: {len(executions)}, passed {len(good)}, failed {len(executions) - len(good)}")
    for location in sorted({e.location for e in executions}):
        print(f"  {location}: passed {by_location[location, True]}, failed {by_location[location, False]}")
    availability = len(good) / len(executions)
    print(f"Probe availability: {len(good)} / {len(executions)} = {availability:.2%} (target {AVAILABILITY_TARGET:.0%})")
    met = availability >= AVAILABILITY_TARGET
    if good:
        latency = len(fast) / len(good)
        met = met and latency >= LATENCY_TARGET
        print(
            f"Probe latency (passed within {LATENCY_THRESHOLD_MS} ms): {len(fast)} / {len(good)} = "
            f"{latency:.2%} (target {LATENCY_TARGET:.0%})",
        )
    failed_times = sorted({e.time for e in executions if not e.passed})
    if failed_times:
        print(f"Failed executions between {failed_times[0]} and {failed_times[-1]}")

    logged = sum(server.values())
    print(f"Server's view (Cloud Run request log, probe requests only): {logged} logged")
    print("  statuses: " + (", ".join(f"{s} x{n}" for s, n in sorted(server.items())) or "none"))
    print(f"  executions with no matching log entry: {max(len(executions) - logged, 0)}")
    return 0 if met else 1


def main() -> int:
    """Parse arguments, read both views of the window and print the report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--service", required=True, help="e.g. cloud-storage-service-demo")
    parser.add_argument("--days", type=int, default=7, help="window ending now (default 7)")
    parser.add_argument("--start", help="window start, UTC, e.g. 2026-11-11T22:40:00Z")
    parser.add_argument("--end", help="window end, UTC (default now)")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.UTC)
    end = args.end or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = args.start or (now - datetime.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check_id = find_check(args.project, args.service, _token())
    print(f"Service: {args.service}  probe GET /files/metadata?path={PROBE_PATH}  check {check_id}")
    print(f"Window: {start} to {end} (results and log entries can take a few minutes to arrive)")
    return report(
        read_executions(args.project, check_id, start, end),
        read_server_view(args.project, args.service, start, end),
    )


if __name__ == "__main__":
    sys.exit(main())
