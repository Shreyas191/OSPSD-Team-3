"""S1 task evaluation (#103): set up a course from its syllabus, end to end, and score the result.

Each case in ``s1_eval_cases.json`` is a labeled syllabus. A run plays the whole of S1 with
the real model (or the scripted one, to check the harness):

1. the student asks for the setup (``--wording baseline`` or ``fresh``);
2. if the assistant asks a question, the student answers with the case's ``answer`` (at
   most twice);
3. the person approves the proposal and it runs;
4. the result is scored: every labeled deadline must be an event on the right local date
   and time (a deadline's event starts or ends at the due time; an exam's starts and ends
   as labeled), with a card for it, plus a ``course plan.md`` document. A record the student
   already had must be untouched, and nothing may be updated or deleted.

Both services run in this process, as in ``tests/test_agent_with_api.py``: the real API app on
SQLite, the agent app, and in-memory providers.

Run::

    GEMINI_API_KEY=... uv run python scripts/s1_eval.py --wording baseline --out before.json

It prints one row per case (success, time, model calls, tokens, failures, model version,
prompt version) and, with ``--out``, writes them as JSON. No syllabus or message text is
written; the syllabi are in ``s1_eval_cases.json`` already.
"""

# ruff: noqa: T201, INP001 - a command-line script that prints its results

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from calendar_contract import CreateEvent
from cloud_storage_service.app import app as api_app
from cloud_storage_service.database import get_database
from cloud_storage_service.dependencies import get_calendar, get_document_storage, get_issue_tracker
from cloud_storage_service.fake_verticals import (
    InMemoryCalendar,
    InMemoryDocumentStorage,
    InMemoryIssueTracker,
)
from cloud_storage_service.identity import DEV_SIGN_IN_ENV_VAR
from course_agent_service.config import Settings, build_model
from course_agent_service.loop import SYSTEM_PROMPT
from course_agent_service.model import Message, ModelClient, ModelReply, ToolSpec
from course_agent_service.tools import TOOL_SPECS
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from course_agent_service import app as agent_app
from course_autopilot_store import Database

ALICE = {"X-Dev-User": "alice"}
HERE = Path(__file__).parent
CASES = json.loads((HERE / "s1_eval_cases.json").read_text(encoding="utf-8"))
MAX_ANSWERS = 2
# Seconds to wait before each real model call, to stay under the free tier's 15 per minute.
PACE_SECONDS = 4.5
VERBOSE = False


def prompt_version() -> str:
    """Return a short hash of what the model is told: the system prompt and the tool specs."""
    specs = [{"name": s.name, "description": s.description, "parameters": s.parameters} for s in TOOL_SPECS]
    text = SYSTEM_PROMPT + json.dumps(specs, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:8]


class Paced:
    """Wraps the real model, pacing its calls."""

    def __init__(self, model: ModelClient) -> None:
        """Wrap ``model``."""
        self.model = model

    def complete(self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelReply:
        """Wait, then ask the wrapped model."""
        time.sleep(PACE_SECONDS)
        return self.model.complete(system=system, messages=messages, tools=tools)


@dataclass
class Run:
    """One case's result."""

    case: str
    success: bool
    seconds: float
    model_calls: int
    tokens: int
    turns: int
    stop_reasons: list[str]
    failures: list[str] = field(default_factory=list)
    model_version: str = ""


def local(value: str, zone: str) -> datetime:
    """Return an event time as local wall-clock time in ``zone``."""
    return datetime.fromisoformat(value).astimezone(ZoneInfo(zone))


def _at_right_time(event: Any, item: dict[str, str], zone: str) -> bool:  # noqa: ANN401
    """Whether ``event`` is at the labeled time: an exam's own span, or a deadline's due time."""
    start, end = local(event.start, zone), local(event.end, zone)
    if "due" in item:
        # A deadline's event starts or ends at the due time, on the due date.
        return any(t.date().isoformat() == item["date"] and t.strftime("%H:%M") == item["due"] for t in (start, end))
    return (
        start.date().isoformat() == item["date"]
        and start.strftime("%H:%M") == item["start"]
        and end.strftime("%H:%M") == item["end"]
    )


def score(
    case: dict[str, Any],
    calendar: InMemoryCalendar,
    issues: InMemoryIssueTracker,
    storage: InMemoryDocumentStorage,
    keep: tuple[str, str],
) -> list[str]:
    """Return every way the result differs from the labels; empty when the setup is right."""
    failures: list[str] = []
    events = [e for k, e in calendar._events.items() if k != keep[0]]  # noqa: SLF001
    titles = [i.title.lower() for k, i in issues._issues.items() if k != keep[1]]  # noqa: SLF001
    zone = case["zone"]
    for item in case["expected"]:
        keyword = item["keyword"]
        matches = [e for e in events if keyword in e.title.lower()]
        if not matches:
            failures.append(f"no event for {keyword}")
        elif not any(_at_right_time(e, item, zone) for e in matches):
            found = ", ".join(f"{local(e.start, zone):%m-%d %H:%M}-{local(e.end, zone):%H:%M}" for e in matches)
            failures.append(f"{keyword} at the wrong time ({found})")
        if not any(keyword in t for t in titles):
            failures.append(f"no card for {keyword}")
    if f"/Course Autopilot/{case['course']}/course plan.md" not in storage.files:
        failures.append("no course plan.md")
    if keep[0] not in calendar._events or keep[1] not in issues._issues:  # noqa: SLF001
        failures.append("an existing record was changed or removed")
    return failures


def _converse(agent: TestClient, cid: str, texts: list[str]) -> tuple[list[str], str | None]:
    """Send the request, then the labeled answer to each question; return stops and proposal."""
    stops: list[str] = []
    for text in texts:
        body = agent.post(f"/conversations/{cid}/turns", json={"text": text}, headers=ALICE).json()
        stops.append(str(body.get("stop_reason", body.get("error", {}).get("code"))))
        if body.get("proposal_id"):
            return stops, str(body["proposal_id"])
        if body.get("stop_reason") != "replied":
            break
    return stops, None


def _approve_and_run(api: TestClient, proposal_id: str) -> list[str]:
    """Approve the proposal as the person and run it; return what went wrong, if anything."""
    failures: list[str] = []
    proposal = api.get(f"/proposals/{proposal_id}", headers=ALICE).json()
    if any(c["op"].startswith(("update", "delete")) for c in proposal["changes"]):
        failures.append("proposed an update or delete")
    api.post(f"/proposals/{proposal_id}/approve", json={"changes_hash": proposal["changes_hash"]}, headers=ALICE)
    done = api.post(f"/proposals/{proposal_id}/execute", headers=ALICE).json()
    if done.get("status") != "done":
        failures.append(f"execution {done.get('status')}")
    return failures


def run_case(case: dict[str, Any], model: ModelClient, request: str) -> Run:
    """Play S1 for one case and score it."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    database = Database(engine)
    database.upgrade()
    storage, issues, calendar = InMemoryDocumentStorage(), InMemoryIssueTracker(), InMemoryCalendar()
    storage.files[f"/Course Autopilot/{case['course']}/{case['file']}"] = case["text"].encode()
    # Records the student already has, which no setup may touch.
    dentist = calendar.create(CreateEvent(title="Dentist", start="2027-02-05T15:00:00Z", end="2027-02-05T16:00:00Z")).id
    groceries = issues.create_issue("Buy groceries").id
    api_app.dependency_overrides[get_database] = lambda: database
    api_app.dependency_overrides[get_document_storage] = lambda: storage
    api_app.dependency_overrides[get_issue_tracker] = lambda: issues
    api_app.dependency_overrides[get_calendar] = lambda: calendar
    api = TestClient(api_app)
    agent_app.app.dependency_overrides[agent_app.get_model] = lambda: model
    agent_app.app.dependency_overrides[agent_app.get_api_http] = lambda: api
    agent = TestClient(agent_app.app)

    started = time.monotonic()
    cid = agent.post("/conversations", json={}, headers=ALICE).json()["id"]
    stops, proposal_id = _converse(agent, cid, [request.format(course=case["course"])] + [case["answer"]] * MAX_ANSWERS)
    failures = _approve_and_run(api, proposal_id) if proposal_id else [f"no proposal ({stops[-1]})"]
    seconds = time.monotonic() - started
    failures += [f for f in score(case, calendar, issues, storage, (dentist, groceries)) if proposal_id or "no " not in f]
    if VERBOSE:
        for m in api.get(f"/conversations/{cid}/messages", headers=ALICE).json():
            content = m.get("content") or {}
            if m.get("role") == "tool" and content.get("name") == "propose_changes":
                print(f"  {case['id']}: propose_changes -> {content.get('text', '')[:300]}", file=sys.stderr)
            if m.get("role") == "assistant" and content.get("text"):
                print(f"  {case['id']}: says: {content['text'][:200]}", file=sys.stderr)
    records = [
        (m.get("content") or {}).get("run") or {}
        for m in api.get(f"/conversations/{cid}/messages", headers=ALICE).json()
        if m.get("role") == "assistant"
    ]
    api_app.dependency_overrides.clear()
    agent_app.app.dependency_overrides.clear()
    engine.dispose()
    return Run(
        case=case["id"],
        success=not failures,
        seconds=round(seconds, 1),
        model_calls=sum(r.get("model_calls", 0) for r in records),
        tokens=sum(r.get("input_tokens", 0) + r.get("output_tokens", 0) for r in records),
        turns=len(stops),
        stop_reasons=stops,
        failures=failures,
        model_version=next((r["model_version"] for r in records if r.get("model_version")), ""),
    )


def main() -> None:
    """Run every case once with the real model; print and optionally save the table."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--wording", choices=["baseline", "fresh"], default="baseline")
    parser.add_argument("--only", help="run only these case ids, comma-separated")
    parser.add_argument("--out", help="write the results as JSON here")
    parser.add_argument("--verbose", action="store_true", help="print replies and refusals (stderr only)")
    args = parser.parse_args()
    global VERBOSE  # noqa: PLW0603 (a command-line switch)
    VERBOSE = args.verbose
    os.environ[DEV_SIGN_IN_ENV_VAR] = "enabled"
    os.environ.pop("K_SERVICE", None)
    real = build_model(Settings.from_env({**os.environ, "MODEL_PROVIDER": "gemini"}))
    assert real is not None
    model = Paced(real)
    chosen = set(args.only.split(",")) if args.only else None
    request = CASES["requests"][args.wording]
    runs = [run_case(c, model, request) for c in CASES["cases"] if chosen is None or c["id"] in chosen]

    version = prompt_version()
    print(f"Prompt version {version}, wording '{args.wording}': \"{request}\"\n")
    print("| Case | Success | Time (s) | Model calls | Tokens | Turns | Stop reasons | Failures | Model |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        print(
            f"| {r.case} | {'yes' if r.success else '**no**'} | {r.seconds} | {r.model_calls} | {r.tokens} | {r.turns} "
            f"| {', '.join(r.stop_reasons)} | {'; '.join(r.failures) or '—'} | {r.model_version} |",
        )
    print(f"\n{sum(r.success for r in runs)} of {len(runs)} succeeded.")
    if args.out:
        result = {"prompt_version": version, "wording": args.wording, "request": request, "runs": [r.__dict__ for r in runs]}
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    sys.exit(0)


if __name__ == "__main__":
    main()
