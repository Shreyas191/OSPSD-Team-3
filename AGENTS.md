# AGENTS.md

Guide for contributors (people and coding agents) working in this repository. Read this first, then follow the links; this file does not repeat what other docs already cover.

**Vertical:** Document Storage · **Provider:** Dropbox · **Language:** Python 3.11 (`uv` workspace)

## Code map

| Path | Owns | Knows about Dropbox? |
|------|------|----------------------|
| `src/cloud_storage_client_api/` | The contract: abstract `Client` and `File` classes and the `get_client()` factory | No |
| `src/dropbox_client_impl/` | `DropboxClient` (implements `Client`), `DropboxFile` (implements `File`), OAuth in `auth.py`, `register()` that binds `get_client()` to Dropbox | **Yes, the only package that does** |
| `src/cloud_storage_service/` | FastAPI app exposing `Client` operations over HTTP | No |
| `tests/integration/` | Tests against the real Dropbox API (skip without credentials) | Yes |
| `tests/e2e/` | Runs `main.py` as a whole | Yes |
| `src/*/tests/` | Fast unit tests for each package (Dropbox SDK mocked) | Mocked only |
| `docs/` | MkDocs site: [testing](docs/testing.md), [component layout](docs/component.md), [CI setup](docs/circleci-setup.md), [workflow](docs/workflow.md) | — |

A request flows: HTTP route (`cloud_storage_service`) → `Client` method (contract) → `DropboxClient` → Dropbox SDK → Dropbox, and the response comes back as a `File`, then a JSON response model.

## Architectural constraints

These are boundaries reviewers will check. Don't break them without discussing it in an issue first.

1. **Dropbox types stay inside `dropbox_client_impl`.** The contract and service packages must never import `dropbox`. Wrap SDK metadata in `DropboxFile` before returning it.
2. **Translate provider errors at the boundary.** `DropboxClient` turns Dropbox errors into the built-in exceptions documented on the `Client` method (`FileNotFoundError`, `FileExistsError`, `ValueError`). The service maps those to HTTP status codes. Never let an `ApiError` decide the HTTP response directly.
3. **Code depends on the contract, not the implementation.** Use `cloud_storage_client_api.get_client()`; importing `dropbox_client_impl` registers the Dropbox implementation. In the service, get the client through the `get_storage_client` dependency so tests can override it.
4. **Credentials come only from `dropbox_client_impl.auth`.** No other module reads tokens or environment variables for Dropbox.
5. **Keep the public contract stable.** Changing a `Client` method signature or an HTTP request/response shape is a contract change: open an issue, agree on it as a team, and update docs and tests in the same PR.
6. **Only add what's needed now.** No speculative abstractions, retries, or features beyond the current level of the spec.

## Setup and checks

Full setup (installing `uv`, creating the Dropbox app, OAuth login) is in the [README](README.md#project-setup). The short version:

```bash
uv sync --all-packages --extra dev               # install everything from uv.lock
uv run python -m dropbox_client_impl login       # one-time Dropbox OAuth login
uv run uvicorn cloud_storage_service.app:app --reload   # start the service (Swagger UI at /docs)
```

Run these before every push; CI runs the same checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest src/ tests/ -m "not local_credentials"
```

## Test boundaries

See [docs/testing.md](docs/testing.md) for markers and commands.

- **Unit tests (`src/*/tests/`)** must not touch the network or need credentials. Mock `client.dbx` for the Dropbox client, and override `get_storage_client` for HTTP routes. Coverage must stay at or above 85%.
- **Integration tests (`tests/integration/`)** hit real Dropbox. Skip them when no credentials are available, and create and delete everything inside a uniquely named scratch folder so the account is left unchanged.
- Test public behaviour (HTTP status codes and response bodies, `File` fields, raised exceptions), not private helpers.

## Contribution rules

The full process is in [docs/workflow.md](docs/workflow.md). In short:

- Every change starts from a GitHub issue with an owner.
- Branch from `dev` as `<name>-<feature>` (e.g. `shreyas-update-apis`), and open a PR into `dev` using the PR template, linking the issue (`Closes #N`).
- At least one teammate other than the author reviews and approves before merging. CI must be green.
- Keep PRs focused: one feature, its tests and its docs. No unrelated edits.
- Never commit secrets: `.env`, `.dropbox_token.json` and tokens stay local (they are gitignored).
- Remove unused code and dependencies. Add a dependency only with a stated reason in the PR.
- Say in the PR description when code was generated with an AI tool. The author still owns, understands and verifies every line.

## Notes for coding agents

- Make bounded changes with a clear acceptance criterion (usually an issue); don't refactor outside its scope.
- Verify claims about Dropbox behaviour against the [Dropbox API docs](https://www.dropbox.com/developers/documentation/http/documentation) or an integration test, not memory.
- Run the full check list above and report any failures honestly instead of skipping or weakening checks.
- Never print, log or commit tokens, and never create, move or delete Git tags or releases unless asked.
