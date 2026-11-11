"""Run the shared conformance suite against the app under each injected fault.

    uv run python scripts/fault_matrix.py /path/to/ospsd26-cloud-storage

For each fault in ``FAULTS`` it starts ``scripts/serve_in_memory.py`` (the real app on an
in-memory provider, no network or credentials) with that fault set, runs the conformance
suite against it, and records which tests passed and every status code and error code the
service answered with (from its one log line per request). It prints a Markdown table.

The suite directory is a checkout of the shared contract repository with ``conformance/``
(see docs/testing.md); its ``httpx2`` import is pointed at our locked ``httpx``.
"""

# ruff: noqa: T201, INP001, S603 - a command-line script; it runs our own server and pytest

import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = 8150

# (label, environment) pairs: no fault first, as the baseline.
FAULTS: list[tuple[str, dict[str, str]]] = [
    ("none", {}),
    ("latency=300", {"FAULT_INJECTION": "latency=300"}),
    ("unavailable", {"FAULT_INJECTION": "unavailable"}),
    ("error", {"FAULT_INJECTION": "error"}),
    ("timeout=1000", {"FAULT_INJECTION": "timeout=1000"}),
    ("lost_response", {"FAULT_INJECTION": "lost_response"}),
    ("error, download only", {"FAULT_INJECTION": "error", "FAULT_OPERATIONS": "download_file"}),
    ("unavailable, 30% of calls", {"FAULT_INJECTION": "unavailable", "FAULT_RATE": "0.3"}),
]

_LINE = re.compile(r"status=(\d+) code='([a-z_]+)'")


@dataclass(frozen=True)
class Result:
    """The outcome of the suite under one fault."""

    label: str
    total: int
    failed: list[str]
    answers: Counter[str]
    seconds: float


def run(suite: Path, label: str, env: dict[str, str]) -> Result:
    """Run the suite under one fault and summarise it."""
    with tempfile.TemporaryDirectory() as tmp:
        server_log = Path(tmp) / "server.log"
        junit = Path(tmp) / "junit.xml"
        with server_log.open("w") as log:
            server = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "serve_in_memory.py"), str(PORT)],
                env={**os.environ, **env},
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            for _ in range(60):
                try:
                    httpx.get(f"http://127.0.0.1:{PORT}/health")
                    break
                except httpx.TransportError:
                    time.sleep(0.25)
            shim = Path(tmp) / "shim"
            shim.mkdir()
            (shim / "httpx2.py").write_text("import sys\n\nimport httpx\n\nsys.modules[__name__] = httpx\n")
            started = time.perf_counter()
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(suite / "conformance" / "tests"),
                    "-c",
                    os.devnull,
                    "--rootdir",
                    str(suite),
                    "-p",
                    "no:cacheprovider",
                    "-q",
                    f"--junitxml={junit}",
                ],
                env={
                    **os.environ,
                    "CONFORMANCE_BASE_URL": f"http://127.0.0.1:{PORT}",
                    "PYTHONPATH": os.pathsep.join([str(shim), str(suite), str(suite / "src" / "cloud_storage_api" / "src")]),
                },
                capture_output=True,
                check=False,
            )
            seconds = time.perf_counter() - started
        finally:
            server.terminate()
            server.wait(timeout=10)

        cases = ET.parse(junit).getroot().iter("testcase")  # noqa: S314
        failed = [c.get("name", "") for c in cases if c.find("failure") is not None or c.find("error") is not None]
        total = sum(1 for _ in ET.parse(junit).getroot().iter("testcase"))  # noqa: S314
        answers = Counter(f"{status} {code}" for status, code in _LINE.findall(server_log.read_text()))
    return Result(label, total, failed, answers, seconds)


def main() -> None:
    """Run every fault and print the results as a Markdown table."""
    if len(sys.argv) != 2:  # noqa: PLR2004
        sys.exit(__doc__)
    suite = Path(sys.argv[1]).resolve()
    results = [run(suite, label, env) for label, env in FAULTS]
    print("| Fault | Conformance passed | Took | Error answers (status code) | 500s |")
    print("|---|---|---|---|---|")
    for r in results:
        errors = ", ".join(f"{k} x{v}" for k, v in sorted(r.answers.items()) if not k.startswith("2"))
        fives = sum(v for k, v in r.answers.items() if k.startswith("500"))
        passed = f"{r.total - len(r.failed)}/{r.total}"
        print(f"| `{r.label}` | {passed} | {r.seconds:.1f} s | {errors or '-'} | {fives} |")
    for r in results:
        if r.failed:
            print(f"\n**{r.label}** failed: {', '.join(sorted(r.failed))}")


if __name__ == "__main__":
    main()
