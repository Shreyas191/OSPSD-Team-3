"""Report the two SLIs for ``GET /files/metadata`` over a time window (docs/slo.md).

    uv run python scripts/slo_report.py --project <project-id> \
        --service cloud-storage-service-demo --days 7
    uv run python scripts/slo_report.py --project <project-id> \
        --service cloud-storage-service-test \
        --start 2026-11-11T22:40:00Z --end 2026-11-11T22:45:00Z

It reads Cloud Run's own request log (``run.googleapis.com/requests``) with
``gcloud logging read``, so it counts every request Cloud Run received, including the
ones answered by a cold start, the overload limit or Cloud Run itself. It prints counts
only, never a URL: request URLs carry file paths in the query string.

Exit status: 0 when both SLOs are met, 1 when one is not, 2 when the window has no
eligible requests (no data is not a pass).
"""

# ruff: noqa: T201, INP001 - a command-line script, not part of a package

import argparse
import collections
import datetime
import json
import subprocess
import sys
from urllib.parse import urlsplit

OPERATION_PATH = "/files/metadata"
# Caller mistakes: the same request would fail against a perfectly healthy service.
EXCLUDED = {400: "invalid_request", 403: "permission_denied"}
# The correct answers: the entry's metadata, or "nothing exists at the path".
ANSWERED = {200, 404}
AVAILABILITY_TARGET = 0.99
LATENCY_THRESHOLD_MS = 1000
LATENCY_TARGET = 0.95
# Below this, one bad request moves the SLI by more than a percentage point.
MIN_ELIGIBLE = 100
NOMINAL_TRAFFIC = 1000


def read_requests(project: str, service: str, start: str, end: str) -> list[tuple[int, float]]:
    """Return ``(status, latency_ms)`` for each GET /files/metadata request in the window."""
    log_filter = " AND ".join(
        [
            f'logName="projects/{project}/logs/run.googleapis.com%2Frequests"',
            'resource.type="cloud_run_revision"',
            f'resource.labels.service_name="{service}"',
            'httpRequest.requestMethod="GET"',
            f'httpRequest.requestUrl:"{OPERATION_PATH}"',
            f'timestamp>="{start}"',
            f'timestamp<"{end}"',
        ],
    )
    output = subprocess.run(  # noqa: S603 - fixed command, arguments are not shell-parsed
        ["gcloud", "logging", "read", log_filter, f"--project={project}", "--format=json"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    requests = []
    for entry in json.loads(output or "[]"):
        http = entry.get("httpRequest", {})
        if urlsplit(http.get("requestUrl", "")).path != OPERATION_PATH:
            continue
        latency_ms = float(http.get("latency", "0s").rstrip("s")) * 1000
        requests.append((int(http.get("status", 0)), latency_ms))
    return requests


def budget_line(target: float, good: int, eligible: int) -> str:
    """Describe the error budget a target allows for ``eligible`` requests, and what is left."""
    allowed = (1 - target) * eligible
    used = eligible - good
    left = allowed - used
    if left < 0:
        return f"error budget: {allowed:.2f} allowed, {used} used: SPENT (over by {-left:.2f})"
    return f"error budget: {allowed:.2f} allowed, {used} used, {left:.2f} left ({left / allowed:.0%})"


def report(requests: list[tuple[int, float]]) -> int:
    """Print both SLIs and return the exit status."""
    statuses = collections.Counter(status for status, _ in requests)
    eligible = [(s, ms) for s, ms in requests if s not in EXCLUDED]
    answered = [ms for s, ms in eligible if s in ANSWERED]
    fast = [ms for ms in answered if ms <= LATENCY_THRESHOLD_MS]

    excluded = len(requests) - len(eligible)
    print(f"Requests: {len(requests)} logged, {len(eligible)} eligible, {excluded} excluded (400/403)")
    print("Statuses: " + ", ".join(f"{s} x{n}" for s, n in sorted(statuses.items())))
    if not eligible:
        print("NO DATA: no eligible requests in the window. This is not a pass.")
        return 2
    if len(eligible) < MIN_ELIGIBLE:
        print(f"LOW TRAFFIC: fewer than {MIN_ELIGIBLE} eligible requests; look at each bad request.")

    met = True
    for name, good, total, target in [
        ("Availability (answered 200 or 404)", len(answered), len(eligible), AVAILABILITY_TARGET),
        (f"Latency (answered within {LATENCY_THRESHOLD_MS} ms)", len(fast), len(answered), LATENCY_TARGET),
    ]:
        if not total:
            print(f"{name}: no answered requests to measure")
            continue
        value = good / total
        met = met and value >= target
        print(f"{name}: {good} / {total} = {value:.2%} (target {target:.0%})")
        print(f"  {budget_line(target, good, total)}")
        print(f"  for {NOMINAL_TRAFFIC} eligible requests: {(1 - target) * NOMINAL_TRAFFIC:.0f} bad allowed")
    return 0 if met else 1


def main() -> int:
    """Parse arguments, read the window's requests and print the report."""
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
    print(f"Service: {args.service}  GET {OPERATION_PATH}")
    print(f"Window: {start} to {end} (log entries can take a minute or two to arrive)")
    return report(read_requests(args.project, args.service, start, end))


if __name__ == "__main__":
    sys.exit(main())
