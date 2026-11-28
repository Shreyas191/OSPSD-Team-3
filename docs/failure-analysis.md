# Failure Analysis

HW3 Milestone 2, Level 5 (#112). For each significant failure on Course Autopilot's path, this page records what students can still do, what becomes uncertain, how we detect it and how work recovers. Representative cases were run, not just reasoned about; the [readiness record](https://github.com/Shreyas191/OSPSD-Team-3/pull/117) links here.

The path a key-scenario request takes (S1, set up a course), and where it can fail:

```
student ─► interface ─► agent service ─► Gemini
                │             │
                │             └──────► API service ─► Neon (database)
                └───────────────────►      │       ─► Dropbox, Trello, Google Calendar (the student's own)
                 (approve, execute)
```

The agent only reads and proposes; the API service alone writes, and only after the student approves the exact changes (DESIGN.md, P1 and P2). So a failure before approval can lose work in progress but can't change anything in a student's accounts. The failures that matter most are the ones during execution, where a provider may already have been written to.

## How the cases were tested

[`scripts/failure_matrix.py`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/scripts/failure_matrix.py) runs S1 end to end in one process: the agent service in front of the real API service app, a SQLite database with our migrations, the in-memory providers and a scripted model (the wiring of `tests/test_agent_with_api.py`). For each case it injects one failure and records what the student sees and the state left behind: the turn's stop reason, HTTP statuses, the proposal's status, each change's outcome, and how many events and cards exist.

It was first run on Nov 26, 2026, before the agent loop (#92) and execution (#89, #90) had merged, from the `shreyas-agent-loop` branch at commit `d99f124`. That run found the five problems listed under [What we learned](#what-we-learned-and-what-should-change).

**Rerun on Nov 27, 2026, on `hw2` at `0e7a6c0`,** after #116 and #124 merged:

```bash
uv run python scripts/failure_matrix.py
```

The script needed one update for the current tool contract: `propose_changes` now takes local event times with a `time_zone`, so its scripted event has no offset. The two cases that used to end stuck now go on to the recovery #116 added. For the stopped instance, the script makes the run look 11 minutes silent, as `test_a_run_that_stops_mid_change_is_taken_over_and_never_repeats_the_write` does, then runs the proposal again. For both, the student then settles the uncertain event with its ID. The unit tests that cover the same behaviour also passed on that commit: 155 tests in `test_execution.py`, `test_proposal_routes.py`, `test_agent.py` and `test_agent_with_api.py`.

Results (run `a0233a35`; a second run, `58c67d8c`, gave the same table):

| Failure (injected) | What the student sees | State afterwards |
|---|---|---|
| None (baseline) | turn `200 proposal_ready`; execute `200` | proposal `done` (completed, completed); 1 event, 1 card |
| Gemini over quota or overloaded (retryable error) | turn `200 model_unavailable`: "The assistant is busy right now; try again in a minute." | no proposal; nothing written |
| Gemini rejects the key (non-retryable error) | turn `200 model_unavailable`: "The assistant failed to answer; try again later." | no proposal; nothing written |
| API service unreachable from the agent | `503 api_unavailable` when starting the conversation | nothing stored |
| Database (Neon) unreachable | starting a conversation: `503 database_unavailable`; listing documents: `503` (Nov 26: `500 internal_error`) | `GET /health/database` `503 database_unavailable`; nothing stored |
| Dropbox down while the agent reads the syllabus | turn `200 api_unavailable`: "I couldn't reach your course data, so I stopped. Nothing was changed." | one model call; nothing written |
| Google Calendar down during execution (before the write), then recovers | execute `200`, proposal `failed` (card completed, event failed) | after recovery, running it again: `done`, 1 event and 1 card: **the card was not made twice** |
| Google Calendar makes the event, but the reply is lost | execute `200`, `failed` (completed, **uncertain**): "The calendar didn't confirm this change; check your calendar before retrying" | running it again: still `failed`, still 1 event: **not retried, no duplicate**. The student checks and settles it with the event's ID: resolve `200`, proposal **`done`**, 1 event and 1 card |
| Trello refuses the card with a rate limit (429) | execute `200`, `failed` (failed): "The issue tracker is unreachable; try again later" | the event after it didn't run; nothing written |
| The API instance stops after the calendar write, before saving its outcome | the request fails; proposal **`executing`** (card completed, event **`started`**) | running it again at once: `409`, still `executing` (the run may be alive). **11 minutes later:** `200`, `failed` (completed, **uncertain**), still 1 event: taken over, not repeated. The student settles it: resolve `200`, proposal **`done`**, 1 event and 1 card |

The last case stands for anything that ends the API request mid-execution: a redeploy, a crash, running out of memory, or Cloud Run's request timeout. The script raises an exception after the provider write; on Cloud Run the caller would see the connection drop (a `5xx` from Cloud Run) rather than a `500` from us, but the state left in the database is the same.

## Resiliency matrix

"Tested" refers to the table above, or to a named existing test; "from the code" means it was read, not run.

| Failure | What students can still do | What becomes uncertain | How we detect it | How work recovers |
|---|---|---|---|---|
| **Gemini over quota or overloaded** (free-tier calls per minute or per day; 503 overload) | Everything except chatting: see their courses and plan, approve and execute proposals already made (the API never calls the model) | Nothing; no proposal is made | Turns ending `model_unavailable`: agent alert, Task success panel | By itself when the per-minute quota resets; the per-day quota resets at midnight Pacific. The admission limits in the readiness record keep us under it. Tested |
| **Gemini rejects our key** (revoked, expired billing) | As above | Nothing | Same alert, every turn failing; the run record shows `model_unavailable` | Operator puts a new key in `gemini-api-key-<env>` and redeploys (runbook). Tested |
| **Gemini slow** | Wait; a turn stops at 60 s with "this turn took too long. Nothing was changed." | Nothing | Latency panel against the 45 s objective; `limit_time` in Task success | Send the message again. `test_the_time_limit` |
| **Agent service crashes or restarts mid-turn** | Approve and execute proposals (the interface calls the API for those, DESIGN.md) | The turn in progress is lost: messages are saved only when a turn ends. If `propose_changes` had already run, a **pending proposal exists that the conversation doesn't mention** | Agent `5xx` (agent alert, second condition); Cloud Run restarts the instance | Send the message again. Earlier turns survive (`test_a_restart_keeps_the_question`). The interface should list pending proposals so an orphan one can still be reviewed (#95); it can't yet, because the API has no route that lists proposals. From the code |
| **API service unreachable** (down, overloaded, bad release) | Nothing; every product operation goes through it | Nothing before approval; during execution, see the last row | API `5xx` alert; synthetic probe; turns ending `api_unavailable` | By itself, or roll back a bad release (runbook). Tested |
| **Neon unreachable** (outage, or waking from suspend) | Nothing: conversations, proposals and outcomes all live there | Nothing for reads; during execution, an outcome that can't be saved is the last row | `GET /health/database` `503` (fails the deploy's smoke check); product routes answer `503 database_unavailable`, so the API `5xx` alert fires and the agent stops the turn | By itself when Neon is back. After 5 idle minutes the free plan suspends the database; the first query then waits for it to wake (measured below). Tested |
| **Dropbox down** (or our token for it rejected) | Approve and execute events and cards; the course plan document fails, and execution stops at the first failed change, so anything after it waits for the next run | Nothing; reads fail before anything happens | API dashboard: `provider_unavailable` errors; synthetic probe; turns ending `api_unavailable` | Send the message again when Dropbox is back. Tested |
| **Google Calendar or Trello down, before the write** | Everything else. Completed changes stay; execution stops at the failed change, so later ones wait for the next run | Nothing: the failed change wasn't written | The proposal's outcomes (`failed`, "unreachable; try again later"), and a `proposal_execution` log line for the run. **Not on any dashboard yet** (see gaps) | Run the proposal again: only changes with no outcome or a failed one run, so nothing is made twice. Tested |
| **Calendar or Trello write made, reply lost** (timeout after the write, or a read-back that differs) | Everything else | **That one change**: it's marked `uncertain`, with "check your calendar before retrying" | The proposal's outcomes, and the run's `proposal_execution` log line (`changes_uncertain`). Not on a dashboard (see gaps) | Never retried automatically, so it is never duplicated. The student checks, then settles it: it happened (with the record's ID, which is read back and must match) or it didn't, so the next run does it. The proposal then reaches `done`. The interface's form for this is in #131. Tested |
| **Trello rate limit** (100 requests per 10 s per token) | Everything else | Nothing: refused before writing | The proposal's outcomes | Run again later. The student is told "unreachable", not "too many requests". Tested |
| **Provider access revoked** (student removed our app, token expired) | Everything that doesn't use that provider | Nothing | Outcome "refused access; reconnect Google Calendar" (or Trello) | Reconnect the provider (#98–#101), then run again. From the code (`test_calendar_errors_become_fixed_messages`) |
| **The API instance stops mid-execution** (redeploy, crash, out of memory, request timeout) | Everything except this proposal | **The change being written when it stopped**: it was saved as `started` before the write, so it's known to be in doubt | The proposal stays `executing` with a `started` change; the takeover run's `proposal_execution` line shows it `uncertain` | Once the run has been silent for 10 minutes, running the proposal again takes it over: the `started` change becomes `uncertain` (never repeated), and the rest run. The student then settles it as above. Retrying sooner answers `409`, since the run may still be alive. Tested |
| **A bad release** (code, prompt or model configuration) | Depends on the bug | Changes a faulty release already made aren't undone | Smoke check before traffic; `5xx` alerts; the release panels | Roll back (runbook, #113). Rollback restores code and configuration, not data |

## What we learned, and what should change

Status after the Nov 27 rerun is in bold at the start of each item.

1. **Fixed (#116). A proposal could get stuck in `executing` forever, with an unrecorded write.** Execution marked the proposal `executing`, then saved each change's outcome only after its provider write. If the request ended in between, nothing moved it out of `executing`, and the write had no record. Now each change is saved as `started` before its write and the run keeps a heartbeat. Once a run has been silent for 10 minutes, running the proposal again takes it over and turns a `started` change into `uncertain` instead of repeating it. The rerun recovered the case with no duplicate event.
2. **Fixed in the API (#116); the interface form is in review (#131). An `uncertain` change couldn't be resolved.** The design is right not to retry it, but nothing let the student say "I checked: it's there" or "it isn't", so the proposal could never reach `done`. `POST /proposals/{id}/changes/{index}/resolve` now does: it happened (with a created record's ID, read back to match) or it didn't. In the rerun, both uncertain cases reached `done`.
3. **Fixed (#116). A database outage was reported as `500 internal_error`.** Every route now maps database errors to `503 database_unavailable`, so the agent stops the turn instead of calling tools that can't succeed.
4. **Half done. Execution outcomes weren't measured.** `POST /proposals/{id}/execute` answers `200` even when a change failed or is uncertain. Each run now writes one `proposal_execution` log line with its final status and how many changes completed, failed and are uncertain (#116). Still to do: a log-based metric, a dashboard panel, and an alert on `changes_uncertain > 0`.
5. **Still open. Rate limits read as outages.** Trello's `429` reaches the student as "unreachable; try again later". Harmless, but "too many changes at once; try again in a minute" would tell them it will work soon.

Items 1 to 3 were the ones that decided whether P3 (keep completed changes, report each one) holds in practice; they are fixed, and the rerun shows it. Item 4's panel and alert, and item 5, can follow.

## Measurements

| When (UTC) | What | Result |
|---|---|---|
| Nov 26 | `GET /health/database` on `cloud-storage-service-test`, three calls in a row | 1.06 s, 0.29 s, 0.25 s |
| Nov 26, 23:24 | The same after 7 idle minutes (Neon's free plan suspends after 5), with the instance warmed first (`GET /health` 0.16 s, 0.14 s) | **1.03 s**, then 0.25 s, 0.29 s |

The first database query after an idle spell costs about 0.8 s more than later ones, even on a warm instance: Neon resuming, a new connection, or both (this test can't tell them apart). It fits within the 5 s first-progress objective, so it needs no workaround; it's one reason a student's first turn after a quiet period is slower.

## Detection: what's watched

- **Agent dashboard** ([`infra/agent_monitoring.tf`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/infra/agent_monitoring.tf)), "Course Autopilot agent: task success": turns per minute by how they ended (task success), failed turns and agent `5xx`, full-turn latency p50 and p95 against the 45 s objective, concurrent turns and instances (active work), and model calls and tokens per turn (model usage). It reads the run record the agent writes for every turn (`"event": "agent_run"`), which carries timing, tokens and the stop reason, never message text.
- **Agent alert**: more than 3 turns ending `model_unavailable` or `api_unavailable` within 5 minutes, or more than 5 agent `5xx` responses; it emails the team when it opens and closes, and links the [runbook](runbook.md#alert-students-turns-are-failing). A separate alert is needed because a failed turn still answers `200`, so the existing API `5xx` alert can't see it.
- **Existing (Milestone 1)**: the API's golden-signals dashboard, the API `5xx` alert, the synthetic probe and the deploy's smoke check.

The dashboard and alert are defined but not yet applied: they need the agent loop deployed (#92) to have data. The alert's test, from firing to clearing, is recorded in the [runbook](runbook.md#alert-test-record) once it has run.
