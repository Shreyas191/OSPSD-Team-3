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
uv run pytest src/ --cov=src --cov-fail-under=85
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

## Conformance checks (the vertical's shared suite)

The Document Storage vertical keeps one HTTP test suite that every team's service must pass. It isn't merged in `Dayana20/ospsd26-cloud-storage` yet (that repository's PR #6), so CI pins Team 6's commit `b77617c`; move the pin in `.circleci/config.yml` when the suite is merged and tagged.

| Where it runs | Against | When | Result on Nov 11, 2026 |
|---|---|---|---|
| CI job `conformance` | Our real HTTP app on `InMemoryClient` (`tests/memory_client.py`), served by `scripts/serve_in_memory.py`; no network, no credentials | Every push and pull request; the release jobs require it | 55 passed |
| By hand | The deployed `test` service, so real Dropbox | Before a release, or to check a deploy | 55 passed |

`InMemoryClient` is not the route-test fake (`tests/fakes.py`). It behaves like Dropbox where the contract can tell: it stores file bytes, creates parent folders, compares paths ignoring case, and stamps every write with the current UTC time and a new `etag`. If the suite fails in CI but passes against the deployed service, the in-memory client is the likely cause, not the service.

To run it yourself (the suite imports `httpx2`, which we point at our locked `httpx` instead of installing it):

```bash
git clone https://github.com/prateeks99/ospsd26-cloud-storage.git /tmp/shared
git -C /tmp/shared checkout b77617c218d345c8cde28dbf08cf4d1a5cbaef38
mkdir -p /tmp/shim && printf 'import sys\n\nimport httpx\n\nsys.modules[__name__] = httpx\n' > /tmp/shim/httpx2.py
uv run python scripts/serve_in_memory.py 8000 &        # or skip this and use the deployed URL below
CONFORMANCE_BASE_URL=http://127.0.0.1:8000 \
PYTHONPATH=/tmp/shim:/tmp/shared:/tmp/shared/src/cloud_storage_api/src \
  uv run pytest /tmp/shared/conformance/tests -c /dev/null --rootdir /tmp/shared -p no:cacheprovider
```

Against the deployed service, set `CONFORMANCE_BASE_URL=https://cloud-storage-service-test-gyesjhieva-uc.a.run.app`. Each test works in its own `/conformance-<random>` folder and deletes it.

## Infrastructure checks

CI job `infra_validate` installs OpenTofu 1.13.9 (verifying its signature) and runs `tofu fmt -check -recursive infra` and `tofu validate` with no backend and no credentials, on every push and pull request. It catches syntax, type and formatting errors; it can't catch what only a `tofu plan` against the real project shows, so infrastructure PRs include the plan in their description (see [Infrastructure](infrastructure.md)).

## How the Dropbox unit tests are organised

`src/dropbox_client_impl/tests/` has one file per `Client` method (`test_upload.py`, `test_copy.py`, `test_list_folder.py`, ...) plus the `test_auth_*.py` files. The `dbx` fixture in `conftest.py` is `create_autospec(dropbox.Dropbox, instance=True)`: a call with arguments the real SDK would reject fails the test, and every success test asserts the exact SDK call. Tests return and raise real SDK objects built with the helpers in `tests/dropbox_sdk.py`, not `Mock()` stand-ins.

## Authentication

Authentication uses Dropbox OAuth 2.0 (see `src/dropbox_client_impl/README.md`):
- The browser-based `login` command runs only when you invoke it. Tests and `DropboxClient` never open a browser or prompt for input.
- `DropboxClient()` uses `DROPBOX_REFRESH_TOKEN`, the saved token file (`.dropbox_token.json`), or the optional `DROPBOX_ACCESS_TOKEN` development fallback, in that order.
- If none of these are available, it fails fast with a `DropboxAuthError` telling you to run `login`. Missing app configuration is named explicitly.
- Tests can bypass authentication with `DropboxClient(dbx=mock)`.

### What the unit tests cover
The auth tests (`src/dropbox_client_impl/tests/test_auth_*.py`: credentials, requests, OAuth and login) never contact Dropbox. Their only network use is a throwaway callback server on `127.0.0.1`. They cover:
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
