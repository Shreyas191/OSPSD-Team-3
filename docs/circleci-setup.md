# CircleCI Setup Guide

This document explains how to configure CircleCI for the Python Application Template project.

## Overview

The CI/CD pipeline includes:

- **Build**: Environment setup with `uv`
- **Lint**: Code quality checks with `ruff`
- **Unit Tests**: Fast tests with 85% coverage requirement
- **CircleCI Tests**: Integration tests without local credentials
- **Integration Tests**: Full API tests with Gmail credentials (protected branches only)

## Quick Setup

### 1. Connect Repository
1. Log in to [CircleCI](https://circleci.com/)
2. Add your repository from "Projects"
3. CircleCI auto-detects `.circleci/config.yml`

### 2. Environment Variables

Create a **Context** named `gmail-client` with:

| Variable | Description |
|----------|-------------|
| `GMAIL_CLIENT_ID` | OAuth2 client ID |
| `GMAIL_CLIENT_SECRET` | OAuth2 client secret |
| `GMAIL_REFRESH_TOKEN` | OAuth2 refresh token |
| `GMAIL_TOKEN_URI` | `https://oauth2.googleapis.com/token` |
| `GMAIL_SCOPES` | `https://www.googleapis.com/auth/gmail.modify` |
| `GMAIL_UNIVERSE_DOMAIN` | `googleapis.com` |

## Workflows

### Standard Workflow (All Branches)
```
build → lint + unit_test → circleci_test → report_summary
```

### Full Integration (main/develop only)
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
uv run mypy src/

# Tests
uv run pytest src/ --cov=src --cov-fail-under=85
uv run pytest src/ tests/ -m "not local_credentials"
```

## Troubleshooting

**"Extra 'dev' is not defined"**: Use `[project.optional-dependencies]` instead of `[dependency-groups]` in `pyproject.toml`

**Missing environment variables**: Ensure `gmail-client` context is created and applied to integration jobs

**Coverage failures**: Project requires 85% coverage - add tests or adjust threshold

**uv command issues**: Use pure `uv` commands (`uv tree`, `uv add`) not `uv pip`

## Security Notes

- Never commit credentials
- Integration tests only run on protected branches (`main`, `develop`)
- Use CircleCI contexts for sensitive variables
