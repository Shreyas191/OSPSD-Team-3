# Container Image and Configuration

This page covers HW2 Milestone 3, Level 1, for two items: building the service as a container image (item 2.1), and the configuration and secrets it needs at run time (item 2.2). The infrastructure that runs the image (Cloud Run, Artifact Registry, service accounts) is item 2.3 and is not described here yet.

## Build the image

```bash
scripts/build_image.sh                 # prints cloud-storage-service:<full commit SHA>
```

The script:

- refuses to build if the working tree has uncommitted or untracked changes, because the image would then not match the commit in its tag;
- tags the image with the full commit SHA and never with `latest`;
- records the same SHA in the image label `org.opencontainers.image.revision`;
- builds for `linux/amd64`, the platform Cloud Run runs. On Apple silicon this runs under emulation; `PLATFORM=linux/arm64 scripts/build_image.sh` builds a native image for a quick local run.

The `Dockerfile` has two stages:

1. **Builder.** It installs the service and its dependencies into `/app/.venv` with `uv sync --locked --no-dev --no-editable --package cloud-storage-service`. `--locked` stops the build if `uv.lock` does not match the `pyproject.toml` files, so the image contains exactly the locked versions.
2. **Runtime.** It copies only that virtual environment and `main.py`, and runs as an unprivileged user (`app`, uid 10001).

Both base images are pinned by digest: `python:3.12-slim-trixie` and `ghcr.io/astral-sh/uv:0.12.10`. To update one, change its tag and digest together in a reviewed pull request; `docker buildx imagetools inspect <tag>` prints the digest.

`.dockerignore` is an allowlist. Only `pyproject.toml`, `uv.lock`, `README.md`, `main.py` and `src/` (without the tests) reach the build, so `.env`, `.dropbox_token.json`, `.git` and `.venv` can never end up in the image.

What we observed on Nov 9, 2026, on the `linux/amd64` image:

- The image is 52.8 MB.
- It contains only `/app/.venv` and `/app/main.py`, and none of the dev tools (pytest, ruff, mypy, mkdocs).
- It runs as uid 10001.
- With credentials passed as environment variables, it answered `GET /openapi.json` and a real `GET /folders/list?path=` with `200`. Its log contained neither the app secret nor the refresh token.

## Run it locally

```bash
docker run --rm -p 8080:8080 \
  -e DROPBOX_APP_KEY -e DROPBOX_APP_SECRET -e DROPBOX_REFRESH_TOKEN \
  cloud-storage-service:<sha>
```

`-e NAME` without a value copies the variable from your shell, so the value never appears on the command line or in your shell history. The service listens on `0.0.0.0:8080` inside the container.

## Configuration

Settings come only from the environment. The image contains none of them.

| Variable | Required | Secret | Meaning |
|---|---|---|---|
| `DROPBOX_APP_KEY` | Yes | Kept in Secret Manager | The Dropbox app's key |
| `DROPBOX_APP_SECRET` | Yes | Yes | The Dropbox app's secret |
| `DROPBOX_REFRESH_TOKEN` | Yes | Yes | Long-lived token from `uv run python -m dropbox_client_impl login` |
| `PORT` | No (the image sets `8080`; Cloud Run sets its own) | No | Port the server listens on |
| `HOST` | No (the image sets `0.0.0.0`) | No | Address the server binds to |
| `MAX_IN_FLIGHT_REQUESTS` | No (default 10) | No | See [Overload Protection](overload-protection.md) |

### The startup check

Before the server starts, `main.py` calls `check_configuration()` in `cloud_storage_service.app`. If a credential is missing, the process prints one line to stderr and exits with status 1, before it accepts any request:

```
Startup failed: Dropbox is not authorized: missing DROPBOX_REFRESH_TOKEN. Set DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN in the environment, or run `uv run python -m dropbox_client_impl login` to connect your Dropbox account.
```

The message names the missing variables and never shows a value. Tests check this: `src/cloud_storage_service/tests/test_startup.py`, `src/dropbox_client_impl/tests/test_auth_credentials.py`, and `tests/e2e/test_main_application.py` for the real process.

The check confirms that the credentials are **present**, not that Dropbox **accepts** them, and it makes no network call. Checking Dropbox is the job of `GET /health/provider`. As a result, a Dropbox outage never stops the service from starting, and the platform does not restart it in a loop.

## Secrets in Google Secret Manager (test and demo)

**Status: planned, not yet run.** This needs the team's Google Cloud project. Its owner is still to be named; see "Deployment tools" in the design document.

Each environment uses its own Dropbox app, so the `test` service cannot reach the `demo` service's app folder. That gives six secrets:

| Secret | Environment variable |
|---|---|
| `dropbox-app-key-test`, `dropbox-app-key-demo` | `DROPBOX_APP_KEY` |
| `dropbox-app-secret-test`, `dropbox-app-secret-demo` | `DROPBOX_APP_SECRET` |
| `dropbox-refresh-token-test`, `dropbox-refresh-token-demo` | `DROPBOX_REFRESH_TOKEN` |

Six versions equal the Secret Manager free tier of 6 active secret versions per month. A rotation adds a version, so destroy the old one afterwards.

Who does what:

- **OpenTofu (item 2.3)** creates the six secrets with no value. It grants each runtime service account `roles/secretmanager.secretAccessor` on its own environment's three secrets only, and maps them into the Cloud Run service as the variables above.
- **A person adds each value by hand**, so values never enter OpenTofu state or CI logs. To store the refresh token from a local login, piped so it is never printed:

  ```bash
  uv run python -m dropbox_client_impl login     # log in to the test app's Dropbox account
  uv run python -c 'import json; print(json.load(open(".dropbox_token.json"))["refresh_token"], end="")' \
    | gcloud secrets versions add dropbox-refresh-token-test --data-file=-
  ```

- **Cloud Run** reads the secrets when an instance starts. After a rotation, deploy a new revision so new instances pick up the new version.
