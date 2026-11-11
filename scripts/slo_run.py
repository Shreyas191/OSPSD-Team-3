"""Send a recorded run of ``GET /files/metadata`` requests to a deployed service (docs/slo.md).

    uv run python scripts/slo_run.py https://<service-url> --requests 300

The run looks like a consumer checking files, one request at a time: it first asks for a
file that doesn't exist yet (404; after an idle period this is the request that waits for
a cold start), uploads it, then asks for its metadata again and again. Every 12th request
asks for a missing file (404, a correct answer) and every 20th leaves out ``path`` (400, a
caller mistake the SLIs exclude). Everything happens in a new ``/slo-run-<random>`` folder,
which is deleted at the end.

It prints the window to pass to ``scripts/slo_report.py`` and what the client saw, so the
two views can be compared.
"""

# ruff: noqa: T201, INP001 - a command-line script, not part of a package

import argparse
import collections
import datetime
import time
import uuid

import httpx

TIMEOUT_SECONDS = 60


def utc_now(later_seconds: int = 0) -> str:
    """Return the current time (plus ``later_seconds``) as an RFC 3339 UTC timestamp."""
    now = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=later_seconds)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    """Run the requests and print the window and the client's view."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("--requests", type=int, default=300, help="metadata requests to send")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between requests")
    args = parser.parse_args()

    root = f"/slo-run-{uuid.uuid4().hex[:12]}"
    path = f"{root}/report.txt"
    seen: collections.Counter[int] = collections.Counter()
    latencies: list[float] = []
    start = utc_now()
    with httpx.Client(base_url=args.base_url, timeout=TIMEOUT_SECONDS) as http:

        def metadata(params: dict[str, str]) -> None:
            began = time.monotonic()
            status = http.get("/files/metadata", params=params).status_code
            latencies.append((time.monotonic() - began) * 1000)
            seen[status] += 1

        try:
            metadata({"path": path})  # doesn't exist yet
            upload = http.post("/files", data={"path": path}, files={"file": ("report.txt", b"q3\n" * 100)})
            print(f"upload {path}: {upload.status_code}")
            for i in range(1, args.requests):
                time.sleep(args.interval)
                if i % 20 == 0:
                    metadata({})
                elif i % 12 == 0:
                    metadata({"path": f"{root}/missing.txt"})
                else:
                    metadata({"path": path})
        finally:
            print(f"cleanup: DELETE {root} -> {http.delete('/files', params={'path': root}).status_code}")
    end = utc_now(later_seconds=1)  # rounded up, so the report's `timestamp < end` keeps the last request

    ordered = sorted(latencies)
    print(f"Window: --start {start} --end {end}")
    print("Client saw: " + ", ".join(f"{s} x{n}" for s, n in sorted(seen.items())))
    print(
        f"Client latency ms: first {latencies[0]:.0f}, p50 {ordered[len(ordered) // 2]:.0f}, "
        f"p95 {ordered[int(len(ordered) * 0.95)]:.0f}, max {ordered[-1]:.0f}",
    )


if __name__ == "__main__":
    main()
