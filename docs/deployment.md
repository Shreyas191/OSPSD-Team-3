# Container Image and Configuration

This page covers HW2 Milestone 3, Level 1, for two items: building the service as a container image (item 2.1), and the configuration and secrets it needs at run time (item 2.2). The infrastructure that runs the image (Cloud Run, Artifact Registry, service accounts, secrets) is item 2.3, in [Infrastructure](infrastructure.md).

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

Both base images are pinned by digest: `python:3.14-slim-trixie` and `ghcr.io/astral-sh/uv:0.12.10`. To update one, change its tag and digest together in a reviewed pull request; `docker buildx imagetools inspect <tag>` prints the digest.

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
| `ISSUE_TRACKER_IMPL` | No (default `fake`) | No | Which issue-tracker implementation to use: `team4` (Team 4's Trello package), `fake` (in memory) or `failing` (every call fails; refused on demo). See `verticals.py` |
| `TRELLO_API_KEY`, `TRELLO_TOKEN`, `TRELLO_LIST_ID` | With `ISSUE_TRACKER_IMPL=team4` | Key and token: yes | Our own Trello test account and the list new issues go in; read by Team 4's package |
| `CALENDAR_IMPL` | No (default `fake`) | No | Which calendar implementation to use: `team5` (Team 5's Google Calendar package), `fake` or `failing` (refused on demo) |
| `CALENDAR_TOKEN_FILE` | With `CALENDAR_IMPL=team5` | The file is | Path of the OAuth token file for our own Google test account (`python -m team5_calendar.authorize`) |
| `DATABASE_URL` | No (without it the service runs with no database, and `GET /health/database` answers 503) | Yes | Postgres connection string of this environment's Neon branch. Migrations run at startup; see `database.py` |
| `DEV_SIGN_IN` | No (off unless `enabled`; always off on demo) | No | Accept the `X-Dev-User` header on product routes as a development user, who runs on the service's shared clients above and has no connections. A bearer token, when sent, always wins. See `identity.py` |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | For sign-in or calendar connections | Secret: yes | The Google OAuth client ("Web application"); see `infra/sign_in.tf` |
| `SIGN_IN_REDIRECT_URI` | For sign-in (without it, `/auth/google/*` answers 503) | No | Where Google sends the browser after sign-in: the interface's callback |
| `MODEL_CALLS_PER_USER_PER_DAY` | No (default 25) | No | Model calls each user may make per day (Pacific time, when Gemini's allowance resets); past it, turns answer `429 daily_limit_reached`. See `usage.py` |
| `MODEL_CALLS_PER_MINUTE` | No (default 15, Gemini's free tier) | No | Model calls the whole project may make per minute, shared by every agent instance; past it, a turn waits for the next minute |
| `EXECUTION` | No (on unless `disabled`) | No | The operator's switch: `disabled` makes `POST /proposals/{id}/execute` answer `503 execution_disabled`, while reads, proposals and approvals keep working. Set it with `execution_disabled = ["demo"]` in OpenTofu, so an apply doesn't undo it; for an immediate stop, `gcloud run services update cloud-storage-service-<env> --region=us-central1 --update-env-vars=EXECUTION=disabled` as well |
| `TOKEN_ENCRYPTION_KEY` | For connections (without it, `/connections` and product routes for signed-in users answer 503) | Yes | Fernet key that encrypts stored provider tokens; see `infra/connections.tf` and `connections.py` |
| `DROPBOX_CONNECT_REDIRECT_URI` | To offer Dropbox connections | No | Where Dropbox sends the browser back; uses `DROPBOX_APP_KEY`/`_SECRET` (an App folder app) |
| `CALENDAR_CONNECT_REDIRECT_URI` | To offer Google Calendar connections | No | Where Google sends the browser back after the `calendar.events` consent; uses the OAuth client above |
| `TRELLO_CONNECT_API_KEY`, `TRELLO_CONNECT_RETURN_URL` | To offer Trello connections | No (the key is public) | The Power-Up's API key, and where Trello returns the browser with the token in the fragment |

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
| `database-url-test`, `database-url-demo` | `DATABASE_URL` |

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
