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

It needs the agent loop (#92) and execution (#89, #90), which were not merged yet, so it was run from a checkout of the `shreyas-agent-loop` branch at commit `d99f124` on Nov 26, 2026. The existing unit tests on that commit that cover the same behaviour also passed (64 tests in `test_agent.py`, `test_execution.py` and `test_agent_with_api.py`). Rerun it after those PRs merge:

```bash
uv run python scripts/failure_matrix.py
```

Results (run `66775cb9`; an earlier run gave the same results):

| Failure (injected) | What the student sees | State afterwards |
|---|---|---|
| None (baseline) | turn `200 proposal_ready`; execute `200` | proposal `done` (completed, completed); 1 event, 1 card |
| Gemini over quota or overloaded (retryable error) | turn `200 model_unavailable`: "The assistant is busy right now; try again in a minute." | no proposal; nothing written |
| Gemini rejects the key (non-retryable error) | turn `200 model_unavailable`: "The assistant failed to answer; try again later." | no proposal; nothing written |
| API service unreachable from the agent | `503 api_unavailable` when starting the conversation | nothing stored |
| Database (Neon) unreachable | starting a conversation: `500 internal_error`; listing documents: `500` | `GET /health/database` `503 database_unavailable`; nothing stored |
| Dropbox down while the agent reads the syllabus | turn `200 api_unavailable`: "I couldn't reach your course data, so I stopped. Nothing was changed." | one model call; nothing written |
| Google Calendar down during execution (before the write), then recovers | execute `200`, proposal `failed` (card completed, event failed) | after recovery, running it again: `done`, 1 event and 1 card: **the card was not made twice** |
| Google Calendar makes the event, but the reply is lost | execute `200`, `failed` (completed, **uncertain**): "The calendar didn't confirm this change; check your calendar before retrying" | running it again: still `failed`, still 1 event: **not retried, no duplicate** |
| Trello refuses the card with a rate limit (429) | execute `200`, `failed` (failed): "The issue tracker is unreachable; try again later" | the event after it didn't run; nothing written |
| The API instance stops after the calendar write, before saving its outcome | the request fails; proposal **`executing`** (card completed, event: no outcome) | running it again: **`409`, still `executing`**; the event exists but isn't recorded |

The last case stands for anything that ends the API request mid-execution: a redeploy, a crash, running out of memory, or Cloud Run's request timeout. The script raises an exception after the provider write; on Cloud Run the caller would see the connection drop (a `5xx` from Cloud Run) rather than a `500` from us, but the state left in the database is the same.

## Resiliency matrix

"Tested" refers to the table above, or to a named existing test; "from the code" means it was read, not run.

| Failure | What students can still do | What becomes uncertain | How we detect it | How work recovers |
|---|---|---|---|---|
| **Gemini over quota or overloaded** (free-tier calls per minute or per day; 503 overload) | Everything except chatting: see their courses and plan, approve and execute proposals already made (the API never calls the model) | Nothing; no proposal is made | Turns ending `model_unavailable`: agent alert, Task success panel | By itself when the per-minute quota resets; the per-day quota resets at midnight Pacific. The admission limits in the readiness record keep us under it. Tested |
| **Gemini rejects our key** (revoked, expired billing) | As above | Nothing | Same alert, every turn failing; the run record shows `model_unavailable` | Operator puts a new key in `gemini-api-key-<env>` and redeploys (runbook). Tested |
| **Gemini slow** | Wait; a turn stops at 60 s with "this turn took too long. Nothing was changed." | Nothing | Latency panel against the 45 s objective; `limit_time` in Task success | Send the message again. `test_the_time_limit` |
| **Agent service crashes or restarts mid-turn** | Approve and execute proposals (the interface calls the API for those, DESIGN.md) | The turn in progress is lost: messages are saved only when a turn ends. If `propose_changes` had already run, a **pending proposal exists that the conversation doesn't mention** | Agent `5xx` (agent alert, second condition); Cloud Run restarts the instance | Send the message again. Earlier turns survive (`test_a_restart_keeps_the_question`). The interface should list pending proposals so an orphan one can still be reviewed (#95). From the code |
| **API service unreachable** (down, overloaded, bad release) | Nothing; every product operation goes through it | Nothing before approval; during execution, see the last row | API `5xx` alert; synthetic probe; turns ending `api_unavailable` | By itself, or roll back a bad release (runbook). Tested |
| **Neon unreachable** (outage, or waking from suspend) | Nothing: conversations, proposals and outcomes all live there | Nothing for reads; during execution, an outcome that can't be saved is the last row | `GET /health/database` `503` (fails the deploy's smoke check); product routes answer `500`, so the API `5xx` alert fires | By itself when Neon is back. After 5 idle minutes the free plan suspends the database; the first query then waits for it to wake (measured below). Tested |
| **Dropbox down** (or our token for it rejected) | Approve and execute events and cards; the course plan document fails, and execution stops at the first failed change, so anything after it waits for the next run | Nothing; reads fail before anything happens | API dashboard: `provider_unavailable` errors; synthetic probe; turns ending `api_unavailable` | Send the message again when Dropbox is back. Tested |
| **Google Calendar or Trello down, before the write** | Everything else. Completed changes stay; execution stops at the failed change, so later ones wait for the next run | Nothing: the failed change wasn't written | The proposal's outcomes (`failed`, "unreachable; try again later"). **Not on any dashboard yet** (see gaps) | Run the proposal again: only changes with no outcome or a failed one run, so nothing is made twice. Tested |
| **Calendar or Trello write made, reply lost** (timeout after the write, or a read-back that differs) | Everything else | **That one change**: it's marked `uncertain`, with "check your calendar before retrying" | The proposal's outcomes. Not on a dashboard (see gaps) | Never retried automatically, so it is never duplicated. **But the student can't resolve it**: the proposal stays `failed` however often it runs (see gaps). Tested |
| **Trello rate limit** (100 requests per 10 s per token) | Everything else | Nothing: refused before writing | The proposal's outcomes | Run again later. The student is told "unreachable", not "too many requests". Tested |
| **Provider access revoked** (student removed our app, token expired) | Everything that doesn't use that provider | Nothing | Outcome "refused access; reconnect Google Calendar" (or Trello) | Reconnect the provider (#98–#101), then run again. From the code (`test_calendar_errors_become_fixed_messages`) |
| **The API instance stops mid-execution** (redeploy, crash, out of memory, request timeout) | Everything except this proposal | **The change being written when it stopped**: it may exist, and nothing records it | Nothing distinguishes it today; the request just fails | **None. The proposal stays `executing` and every retry answers `409`.** Tested; this is the most damaging gap |
| **A bad release** (code, prompt or model configuration) | Depends on the bug | Changes a faulty release already made aren't undone | Smoke check before traffic; `5xx` alerts; the release panels | Roll back (runbook, #113). Rollback restores code and configuration, not data |

## What we learned, and what should change

1. **A proposal can get stuck in `executing` forever, with an unrecorded write.** Execution marks the proposal `executing`, then saves each change's outcome only after its provider write. If the request ends in between, nothing ever moves it out of `executing`, and the write that happened has no record, so a later "remove this event" would be refused as not ours. Suggested fix (#90): save a `started` outcome before each write; let a proposal that has been `executing` for longer than the API's request timeout run again, treating any `started` change as `uncertain`. Until then, a stuck proposal needs a person: check the provider and fix the row by hand.
2. **An `uncertain` change can't be resolved.** The design is right not to retry it, but there's no way for the student to say "I checked: it's there" or "it isn't", so the proposal can never reach `done`. Suggested fix (#90, #95): an action on an uncertain change that marks it confirmed or failed after the student has looked.
3. **A database outage is reported as `500 internal_error`.** Only `GET /health/database` says `503 database_unavailable`. The agent treats `503` as "stop the turn" but `500` as a refusal it can work around, so during a Neon outage the model keeps calling tools that can't succeed. Suggested fix: map database errors on every route to `503 database_unavailable`.
4. **Execution outcomes aren't measured.** `POST /proposals/{id}/execute` answers `200` even when a change failed or is uncertain, so neither the dashboards nor the alerts see execution failures. Suggested fix (#90): one log line per execution with its final status and how many changes completed, failed and are uncertain; then a dashboard panel and an alert on `uncertain`.
5. **Rate limits read as outages.** Trello's `429` reaches the student as "unreachable; try again later". Harmless, but "too many changes at once; try again in a minute" would tell them it will work soon.

Items 1 and 2 decide whether P3 (keep completed changes, report each one) holds in practice, so they should be fixed before launch week; 3 to 5 can follow.

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
