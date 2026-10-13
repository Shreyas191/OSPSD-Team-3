# Testing Guide

This document explains the testing strategy and how to run different types of tests.

## Test Markers

The project uses pytest markers to categorize tests based on their requirements and suitable environments:

### Core Test Types
- `unit`: Fast, isolated tests that don't require external dependencies
- `integration`: Tests that verify component interactions  
- `e2e`: End-to-end tests that verify the complete application workflow

### Environment-Specific Markers
- `circleci`: Tests that can run in CI/CD environments without local credential files
- `local_credentials`: Tests that require local Dropbox credentials in a `.env` file

## Running Tests

### All Unit Tests (Fast)
```bash
uv run pytest src/ --cov=src --cov-fail-under=90
```

### CircleCI-Compatible Tests Only
```bash
uv run pytest -m circleci
```

### Local Tests Only (Requires Credentials)
```bash
uv run pytest -m local_credentials
```

### Integration Tests
```bash
uv run pytest -m integration
```

### E2E Tests
```bash
uv run pytest -m e2e
```

### Exclude Credential-Dependent Tests
```bash
uv run pytest -m "not local_credentials"
```

## Test Categories by Environment

### CircleCI/CI Environment
Tests marked with `@pytest.mark.circleci` can run in CI environments:
- **Requirements**: None for most tests. The real-Dropbox checks additionally need `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, and `DROPBOX_REFRESH_TOKEN`, and are skipped without them
- **What they test**:
  - Code syntax and imports
  - Factory function dependency injection
  - Authentication flow logic (expects proper failure when credentials are invalid)
  - Application structure integrity
  - Non-interactive authentication mode

Example CircleCI command:
```bash
uv run pytest -m circleci --tb=short
```

### Local Development
Tests marked with `@pytest.mark.local_credentials` require local files:
- **Requirements**: a local `.env` file (see `.env.example`) and a completed OAuth login (`uv run python -m dropbox_client_impl login`)
- **What they test**:
  - Real Dropbox API connectivity
  - End-to-end application functionality

## Environment Variables for CI

Set these environment variables in your CI environment (never in the repository):

```bash
export DROPBOX_APP_KEY="your-app-key"
export DROPBOX_APP_SECRET="your-app-secret"
export DROPBOX_REFRESH_TOKEN="your-refresh-token"
```

To get a refresh token for CI, run `uv run python -m dropbox_client_impl login` locally. Then copy the `refresh_token` value out of `.dropbox_token.json` straight into the CI secret store. Don't paste it anywhere else.

## Authentication

Authentication uses Dropbox OAuth 2.0 (see `src/dropbox_client_impl/README.md`):
- The browser-based `login` command runs only when you invoke it. Tests and `DropboxClient` never open a browser or prompt for input.
- `DropboxClient()` uses `DROPBOX_REFRESH_TOKEN`, the saved token file (`.dropbox_token.json`), or the optional `DROPBOX_ACCESS_TOKEN` development fallback, in that order.
- If none of these are available, it fails fast with a `DropboxAuthError` telling you to run `login`. Missing app configuration is named explicitly.
- Tests can bypass authentication with `DropboxClient(dbx=mock)`.

### What the unit tests cover
`src/dropbox_client_impl/tests/test_authentication.py` never contacts Dropbox. Its only network use is a throwaway callback server on `127.0.0.1`. It covers:
- the authorization URL (app key, redirect URI, `token_access_type=offline`, CSRF state)
- missing or blank `DROPBOX_APP_KEY` / `DROPBOX_APP_SECRET` / `DROPBOX_REDIRECT_URI`
- exchanging the authorization code and saving the refresh token, with file mode `600`
- denied authorizations, CSRF mismatches, rejected codes, and revoked or expired refresh tokens
- creating the SDK client from a refresh token, plus the access-token fallback
- `check_auth()`, `login`, and `logout`
- that secrets never appear in stdout, logs, or exception messages

Every test runs with an isolated environment and a temporary token file, so your real `.env` and `.dropbox_token.json` are never read.

## Test Examples

### Running Tests Without Network Calls
```bash
# Only run tests that don't make real API calls
uv run pytest -m "unit or (circleci and not local_credentials)"
```

### Running Full Local Test Suite
```bash
# Run all tests including those requiring real credentials
uv run pytest
```

### Debugging Authentication Issues
```bash
# Run only authentication-related tests
uv run pytest -k "auth" -v
```

## Expected Behavior in Different Environments

### Local Development (with credentials)
- All tests should pass
- Real Dropbox API calls succeed

### Local Development (without credentials)  
- Unit tests pass
- Integration/E2E tests skip or fail with clear messages
- No hanging or infinite waits

### CircleCI (with environment variables)
- Tests marked `circleci` pass
- Tests marked `local_credentials` are skipped
- No interactive authentication attempts
- Fast execution (no timeouts)

### CircleCI (without environment variables)
- Tests marked `circleci` skip with clear messages
- No test failures due to missing credentials
- Fast execution
