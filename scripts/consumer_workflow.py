"""Run the vertical's shared consumer workflow against a running storage service.

    uv run python scripts/consumer_workflow.py http://127.0.0.1:8000

The steps are the ones every service in the Document Storage vertical must support,
unchanged: upload, metadata, download and compare bytes, list the folder, move, metadata at
the new path, delete, and metadata returning 404. Each step prints the request, the
expected result and the observed result. Everything happens inside a new
``/workflow-<random>`` folder, which is deleted at the end even if a step fails.

Exit status 0 means every step matched; 1 means one did not. The deploy pipeline can use
the same script as its smoke check.
"""

# ruff: noqa: T201, INP001 - a command-line script, not part of a package

import sys
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

TIMEOUT_SECONDS = 60
DATA = b"quarterly report\n" * 1000


@dataclass(frozen=True)
class Step:
    """One request of the workflow and what it should return."""

    name: str
    method: str
    url: str
    expected_status: int
    params: dict[str, str] | None = None
    json: dict[str, str] | None = None
    files: dict[str, tuple[str, bytes]] | None = None
    data: dict[str, str] | None = None


def run(base_url: str) -> bool:
    """Run every step against ``base_url`` and return whether all of them matched."""
    root = f"/workflow-{uuid.uuid4().hex[:12]}"
    path = f"{root}/reports/q3.txt"
    new_path = f"{root}/archive/q3-final.txt"
    with httpx.Client(base_url=base_url, timeout=TIMEOUT_SECONDS) as http:
        try:
            return _run_steps(http, root, path, new_path)
        finally:
            cleanup = http.delete("/files", params={"path": root})
            print(f"cleanup: DELETE {root} -> {cleanup.status_code}")


def _run_steps(http: httpx.Client, root: str, path: str, new_path: str) -> bool:
    checks: list[tuple[Step, Any]] = [
        (
            Step("upload", "POST", "/files", 201, files={"file": ("q3.txt", DATA)}, data={"path": path}),
            lambda body: body["path"] == path and body["size"] == len(DATA),
        ),
        (
            Step("metadata", "GET", "/files/metadata", 200, params={"path": path}),
            lambda body: body["size"] == len(DATA) and body["modified_at"].endswith("Z"),
        ),
        (Step("download", "GET", "/files/download", 200, params={"path": path}), lambda content: content == DATA),
        (
            Step("list", "GET", "/files", 200, params={"path": f"{root}/reports"}),
            lambda body: [entry["path"] for entry in body] == [path],
        ),
        (
            Step("move", "PATCH", "/files", 200, params={"path": path}, json={"new_path": new_path}),
            lambda body: body["path"] == new_path,
        ),
        (
            Step("metadata at new path", "GET", "/files/metadata", 200, params={"path": new_path}),
            lambda body: body["size"] == len(DATA),
        ),
        (
            Step("old path is gone", "GET", "/files/metadata", 404, params={"path": path}),
            lambda body: body["error"]["code"] == "not_found",
        ),
        (Step("delete", "DELETE", "/files", 204, params={"path": new_path}), lambda content: content == b""),
        (
            Step("deleted file is 404", "GET", "/files/metadata", 404, params={"path": new_path}),
            lambda body: body["error"]["code"] == "not_found",
        ),
    ]
    for step, body_ok in checks:
        response = http.request(
            step.method,
            step.url,
            params=step.params,
            json=step.json,
            files=step.files,
            data=step.data,
        )
        is_json = response.headers.get("content-type", "").startswith("application/json")
        # A 204 has no body even when it is labelled JSON, so only parse a non-empty body.
        body = response.json() if is_json and response.content else response.content
        matched = response.status_code == step.expected_status and body_ok(body)
        print(
            f"{'PASS' if matched else 'FAIL'} {step.name}: {step.method} {step.url} {step.params or ''}\n"
            f"     expected {step.expected_status}, observed {response.status_code} "
            f"{_summary(body)}",
        )
        if not matched:
            return False
    return True


def _summary(body: object) -> str:
    """Return a short, safe description of a response body for the log."""
    if isinstance(body, bytes):
        return f"({len(body)} bytes)"
    text = str(body)
    return text if len(text) <= 200 else f"{text[:200]}..."  # noqa: PLR2004


if __name__ == "__main__":
    if len(sys.argv) != 2:  # noqa: PLR2004
        sys.exit(__doc__)
    sys.exit(0 if run(sys.argv[1]) else 1)
