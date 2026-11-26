# course-agent-service

The agent service for Course Autopilot (HW3 Milestone 2, Level 3): the layer between the
interface and the API service, where the language model enters the system.

This first version is the skeleton: configuration, `GET /health`, and the model interface
(`ModelClient`) with two implementations, `GeminiModel` (the real model) and `ScriptedModel`
(replies fixed in advance, for tests). The agent loop, tools and approvals come next.

```bash
uv run python agent_main.py        # http://127.0.0.1:8001
```

| Setting | Values | Default |
|---|---|---|
| `MODEL_PROVIDER` | `gemini`, `scripted`, `disabled` | `disabled` |
| `GEMINI_API_KEY` | required when `MODEL_PROVIDER=gemini` | none |
| `GEMINI_MODEL` | Gemini model name | `gemini-flash-lite-latest` |
| `API_BASE_URL` | URL of the API service the agent calls | none |
