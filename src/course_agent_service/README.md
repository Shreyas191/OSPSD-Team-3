# course-agent-service

The agent service for Course Autopilot (HW3 Milestone 2, Level 3): the layer between the
interface and the API service, where the language model enters the system.

- **Routes** (`app.py`): `POST /conversations` starts a conversation; `POST
  /conversations/{id}/turns` with `{"text": ...}` runs one turn and returns `stop_reason`,
  `reply` and, when the turn produced one, the pending `proposal_id`. The interface then shows
  that proposal and approves it through the API service; the agent can't approve anything.
- **Loop** (`loop.py`): calls the model, runs its tool calls, and stops on a reply, a stored
  proposal, an invalid proposal after two corrections, a limit (8 model calls, 60 s, 200,000
  tokens per turn), a model or API failure, or cancellation. The stop reasons are tabled there.
- **Tools** (`tools.py`): `list_courses`, `list_course_documents`, `read_document`,
  `get_course_plan` (reads) and `propose_changes` (stores a pending proposal). Each parses its
  arguments into typed objects before calling the API service.
- **Limits** (#102): at most 3 turns run at once and 10 wait for a slot; past that a turn
  answers `429 assistant_busy`. Before every model call the loop reserves it from the API
  service (`POST /usage/model-calls`), which counts 25 calls per user per day, 500 per day
  for the project and 15 per minute for the project. A full minute makes the turn wait for
  the next one (reported as `waited_ms`); no calls left today gives `429 daily_limit_reached`
  before anything runs, or the `daily_limit` stop partway through a turn.
- **The minute's wait counts against the turn's 60 s.** Three turns at once can use the
  minute's 15 calls in their first seconds; the wait that follows (up to about 40 s) then
  leaves most of them stopping with `limit_time` rather than finishing. We accept that: the
  turn's 60 s keeps p95 latency under the 45 s objective, and a stopped turn changes nothing
  and can be asked again. If turns often stop this way under load, raise the minute's
  allowance (a paid tier) rather than the turn's time limit. The service runs on one instance so the
  admission limit holds across it.
- **Model** (`model.py`, `gemini.py`): `ModelClient`, with `GeminiModel` (the real model) and
  `ScriptedModel` (replies fixed in advance, for tests).

The agent holds no state, no provider credential and no database connection. Each turn loads
the conversation from the API service and appends what it produced, so a restart loses
nothing. It forwards only the caller's identity headers, so the API service decides who the
user is. Each turn's run record (tool outcomes, timing, tokens, stop reason; never message
text or document content) is saved with the reply and logged as one `agent_run` line.

```bash
uv run python agent_main.py        # http://127.0.0.1:8001
```

| Setting | Values | Default |
|---|---|---|
| `MODEL_PROVIDER` | `gemini`, `scripted`, `disabled` | `disabled` |
| `GEMINI_API_KEY` | required when `MODEL_PROVIDER=gemini` | none |
| `GEMINI_MODEL` | Gemini model name (each run records the version that answered) | `gemini-3.5-flash-lite` |
| `API_BASE_URL` | URL of the API service the agent calls | none |
