# CircleCI Setup Guide

This document explains how to configure CircleCI for the Python Application Template project.

## Overview

The CI/CD pipeline includes:

- **Build**: Environment setup with `uv`
- **Lint**: Code quality checks with `ruff`
- **Unit Tests**: Fast tests with 85% coverage requirement
- **CircleCI Tests**: Integration tests without local credentials
- **Integration Tests**: Full API tests with Dropbox credentials (protected branches only)

## Quick Setup

### 1. Connect Repository
1. Log in to [CircleCI](https://circleci.com/)
2. Add your repository from "Projects"
3. CircleCI auto-detects `.circleci/config.yml`

### 2. Environment Variables

Create a **Context** named `dropbox-client` with:

| Variable | Description |
|----------|-------------|
| `DROPBOX_APP_KEY` | App key from the Dropbox App Console (Settings tab) |
| `DROPBOX_APP_SECRET` | App secret from the Dropbox App Console (Settings tab) |
| `DROPBOX_REFRESH_TOKEN` | OAuth refresh token for the account CI should use |

CI cannot complete the browser-based OAuth flow, so it uses a refresh token instead. To get one, run `uv run python -m dropbox_client_impl login` locally while signed in to the Dropbox account CI should use. Then copy the `refresh_token` value from the generated `.dropbox_token.json` into the context. The Dropbox SDK uses it to obtain short-lived access tokens automatically, so it does not expire after a few hours. To rotate it, run `logout` (or remove the app under Dropbox **Settings → Connected apps**), log in again, and update the context.

Without these variables, the real-Dropbox integration tests are skipped and everything else still runs.

## Workflows

### Standard Workflow (all other branches)
```
build → lint + unit_test → circleci_test → report_summary
```

### Full Integration (`main` and `dev` only)
```
build → lint + unit_test → circleci_test → integration_test → report_summary
```

## Local Development

Run the same checks locally:

```bash
# Setup
uv sync --all-packages --extra dev

# Quality checks
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests

# Tests
uv run pytest src/ --cov=src --cov-fail-under=85
uv run pytest src/ tests/ -m "not local_credentials"
```

## Troubleshooting

**"Extra 'dev' is not defined"**: Use `[project.optional-dependencies]` instead of `[dependency-groups]` in `pyproject.toml`

**Missing environment variables**: Ensure `dropbox-client` context is created and applied to integration jobs

**Coverage failures**: Project requires 85% coverage - add tests or adjust threshold

**uv command issues**: Use pure `uv` commands (`uv tree`, `uv add`) not `uv pip`

## Security Notes

- Never commit credentials (`.env` and `.dropbox_token.json` are gitignored)
- Integration tests with real credentials only run on `main` and `dev`
- Use CircleCI contexts for sensitive variables
