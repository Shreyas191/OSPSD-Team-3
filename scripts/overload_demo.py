"""Send bursts of concurrent requests to a running service and summarise the responses.

Usage (with the service running, e.g. ``MAX_IN_FLIGHT_REQUESTS=4 uv run uvicorn ...``)::

    uv run python scripts/overload_demo.py /Docs/report.pdf 4 16 32

Each burst sends that many ``GET /files/metadata`` requests for the given path at the same
moment and prints how many succeeded, how many were shed with ``503 service_busy``, and how
long each kind took. Every 200 is one Dropbox call; a 503 never reaches Dropbox.
"""

# ruff: noqa: T201, INP001 - a command-line script, not part of a package

import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE_URL = "http://127.0.0.1:8000"


def _one_request(client: httpx.Client, path: str, start: threading.Barrier) -> tuple[int, float]:
    start.wait()
    began = time.perf_counter()
    response = client.get("/files/metadata", params={"path": path})
    return response.status_code, (time.perf_counter() - began) * 1000


def burst(path: str, size: int) -> None:
    """Send ``size`` requests at once and print a one-line summary."""
    start = threading.Barrier(size)
    with httpx.Client(base_url=BASE_URL, timeout=60) as client, ThreadPoolExecutor(size) as pool:
        results = list(pool.map(lambda _: _one_request(client, path, start), range(size)))

    counts = Counter(status for status, _ in results)

    def median_ms(code: int) -> str:
        times = [ms for status, ms in results if status == code]
        return f"{statistics.median(times):.0f} ms" if times else "-"

    other = {status: n for status, n in counts.items() if status not in {200, 503}}
    print(
        f"{size:>3} concurrent: {counts[200]:>3} x 200 (median {median_ms(200)}), "
        f"{counts[503]:>3} x 503 (median {median_ms(503)}), other: {other}",
    )


if __name__ == "__main__":
    target, *sizes = sys.argv[1:]
    for size in sizes:
        burst(target, int(size))
