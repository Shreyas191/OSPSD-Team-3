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
- `local_credentials`: Tests that require local `credentials.json` or `token.json` files

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
- **Requirements**: Only environment variables (`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`)
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
- **Requirements**: `credentials.json` or `token.json` files
- **What they test**:
  - Real Gmail API connectivity
  - Interactive authentication flows
  - Full message retrieval and parsing
  - End-to-end application functionality

## Environment Variables for CI

Set these environment variables in your CI environment:

```bash
export GMAIL_CLIENT_ID="your-oauth-client-id"
export GMAIL_CLIENT_SECRET="your-oauth-client-secret"  
export GMAIL_REFRESH_TOKEN="your-refresh-token"
export GMAIL_TOKEN_URI="https://oauth2.googleapis.com/token"  # Optional
```

## Authentication Modes

The application supports two authentication modes:

### Interactive Mode (`interactive=True`)
- Launches browser for OAuth flow
- Requires `credentials.json`
- Used for initial setup and local development
- **Not suitable for CI/CD**

### Non-Interactive Mode (`interactive=False`)  
- Uses environment variables or existing token files
- Never launches browser or prompts for user input
- **Required for CI/CD environments**
- Fails fast with clear error messages when credentials are missing

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
- Real Gmail API calls succeed
- Interactive authentication works

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
