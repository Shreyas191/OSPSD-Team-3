# OSPSD Team 3: A Component-Based Cloud Storage Client

[![CircleCI](https://circleci.com/gh/Shreyas191/OSPSD-Team-3.svg?style=shield)](https://circleci.com/gh/Shreyas191/OSPSD-Team-3)
[![Coverage](https://img.shields.io/badge/coverage-85%2B%25-brightgreen)](https://circleci.com/gh/Shreyas191/OSPSD-Team-3)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

This repository builds a component-based document storage client in Python. It defines a provider-agnostic cloud storage API and implements it on top of the Dropbox API, so files and folders can be created, read, updated, and deleted through one stable interface.

**Vertical:** Document Storage · **Provider:** Dropbox · **Contributor guide:** [`AGENTS.md`](AGENTS.md) · **Workflow:** [`docs/workflow.md`](docs/workflow.md)

The project emphasizes a strict separation of concerns, dependency injection, and a comprehensive, automated toolchain to enforce code quality and best practices.

## Team Members

| Net ID  | Name           |
|---------|----------------|
| zr2197  | Zesan Rahman   |
| jq2272  | Jing Qian      |
| am15464 | Ashik John     |
| sk12898 | Shreyas Kaldate|
| vhw2009 | Victor Wang    |

## Architectural Philosophy

This project is built on the principle of "programming integrated over time." The architecture is designed to combat complexity and ensure the system is maintainable and evolvable.

-   **Component-Based Design:** The system is broken down into distinct, self-contained components. Each component has a single responsibility and can be "forklifted" out of this project to be used in another with minimal effort.
-   **Interface-Implementation Separation:** Every piece of functionality is defined by an abstract **contract** implemented as an ABC (the "what") and fulfilled by a concrete **implementation** (the "how"). This decouples our business logic from specific technologies (like Dropbox).
-   **Dependency Injection:** Implementations are "injected" into the abstract contracts at runtime. This means consumers of the API only ever depend on the stable interface, not the volatile implementation details.

## Core Components

The project is a `uv` workspace containing two packages:

1.  **`cloud_storage_client_api`**: Defines the abstract `Client` and `File` base classes (ABCs). This is the contract for what a cloud storage client can do: upload, create folders, copy, download, get metadata, list, search, rename, move, and delete.
2.  **`dropbox_client_impl`**: Provides the `DropboxClient` class, a concrete implementation that uses the Dropbox Python SDK to perform the actions defined in the `Client` abstraction.

See [`src/dropbox_client_impl/README.md`](src/dropbox_client_impl/README.md) for which team member owns each method and the Dropbox endpoint it maps to.

## Project Structure

```
OSPSD-Team-3/
├── src/                          # Source packages (uv workspace members)
│   ├── cloud_storage_client_api/ # Abstract Client and File base classes (ABCs)
│   └── dropbox_client_impl/      # Dropbox-specific client implementation
├── tests/                        # Integration and E2E tests
│   ├── integration/              # Component integration tests
│   └── e2e/                      # End-to-end application tests
├── docs/                         # Documentation source files
├── .circleci/                    # CircleCI configuration
├── main.py                       # Main application entry point
├── pyproject.toml               # Project configuration (dependencies, tools)
├── uv.lock                      # Locked dependency versions
├── .env                         # Dropbox app config (local only, see .env.example)
└── .dropbox_token.json          # OAuth refresh token written by `login` (local only)
```

## Project Setup

### 1. Prerequisites

-   Python 3.11 or higher
-   `uv` – A fast, all-in-one Python package manager.

### 2. Initial Setup

1.  **Install `uv`:**
    ```bash
    # macOS / Linux
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Windows (PowerShell)
    irm https://astral.sh/uv/install.ps1 | iex
    ```

2.  **Clone the Repository:**
    ```bash
    git clone https://github.com/Shreyas191/OSPSD-Team-3.git
    cd OSPSD-Team-3
    ```

3.  **Create and Sync the Virtual Environment:**
    This single command creates a `.venv` folder and installs all packages (including workspace members and development tools) defined in `uv.lock`.
    ```bash
    uv sync --all-packages --extra dev
    ```

4.  **Connect Your Dropbox Account (OAuth 2.0):**
    -   You need the team's Dropbox app (see [`src/dropbox_client_impl/README.md`](src/dropbox_client_impl/README.md#authentication-oauth-20) for how it is created). Its redirect URI must be `http://localhost:8080/oauth/callback`.
    -   **Required app permissions** (Permissions tab): `account_info.read`, `files.metadata.read`, `files.metadata.write`, `files.content.read`, `files.content.write`. After changing permissions, click **Submit** and run `login` again; existing tokens keep their old permissions.
    -   Use a dedicated test Dropbox account (or an app with **App folder** access), since tests and demos create, move and delete files.
    -   Copy `.env.example` to `.env` and fill in `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, and `DROPBOX_REDIRECT_URI`.
    -   Run `uv run python -m dropbox_client_impl login`. Your browser opens Dropbox; sign in and click **Allow**. The credentials are saved to `.dropbox_token.json` and renewed automatically.
    -   Verify it works: `uv run python -m dropbox_client_impl`
    -   **CI/CD**: set `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, and `DROPBOX_REFRESH_TOKEN` as environment variables instead (see `docs/circleci-setup.md`).
    -   **Important:** `.env` and `.dropbox_token.json` contain secrets, are ignored by `.gitignore`, and must never be committed.

5.  **Activate the Virtual Environment:**
    ```bash
    # macOS / Linux
    source .venv/bin/activate
    # Windows (PowerShell)
    .venv\Scripts\Activate.ps1
    ```

6.  **Check the Setup:**
    Run the main application, which connects to Dropbox and lists the root folder:
    ```bash
    uv run python main.py
    ```

### 3. Cleanup

-   **Test data:** integration tests create a uniquely named `/ospsd-it-…` folder and delete it when they finish. If a run is interrupted, delete any leftover `/ospsd-it-…` folders in Dropbox by hand. Files you create while trying the service manually are not cleaned up automatically.
-   **Credentials:** `uv run python -m dropbox_client_impl logout` revokes the token and deletes `.dropbox_token.json`. Delete `.env` if you no longer need it.
-   **Environment:** remove the virtual environment with `rm -rf .venv`, and tool caches with `rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage`.

## Development Workflow

See [`docs/workflow.md`](docs/workflow.md) for how issues, reviews, merges and releases work, and [`AGENTS.md`](AGENTS.md) for the code map and contribution rules.

All commands should be run from the project root with the virtual environment activated.

### Running the Application

To run the main demonstration script:
```bash
uv run python main.py
```

### Running the Toolchain

-   **Linting & Formatting (Ruff):**
    The project uses Ruff with comprehensive rules configured in `pyproject.toml`.
    ```bash
    # Check for issues
    uv run ruff check .
    # Automatically fix issues
    uv run ruff check . --fix
    # Check formatting
    uv run ruff format --check .
    # Apply formatting
    uv run ruff format .
    ```

-   **Static Type Checking (MyPy):**
    ```bash
    uv run mypy src tests
    ```

-   **Testing (Pytest):**

    I'd recommend only running: `uv run pytest src/ tests/ -m "not local_credentials" -v` for simplicity.

    The project uses a comprehensive testing strategy with different test categories.
    ```bash
    # Run all tests (includes unit, integration, and e2e tests)
    uv run pytest

    # Run only unit tests (fast, no external dependencies - from src/ directories)
    uv run pytest src/

    # Run all tests except those requiring local credential files
    uv run pytest src/ tests/ -m "not local_credentials"

    # Run only integration tests (requires environment variables or credentials)
    uv run pytest -m integration

    # Run only end-to-end tests (requires credentials)
    uv run pytest -m e2e

    # Run only CircleCI-compatible tests (CI/CD environment)
    uv run pytest -m circleci

    # Run tests with coverage reporting
    uv run pytest --cov=src --cov-report=term-missing
    ```

### Viewing Documentation

This project uses MkDocs for documentation.
```bash
# Start the live-reloading documentation server
uv run mkdocs serve
```
Open your browser to `http://127.0.0.1:8000` to view the site.

## Testing Infrastructure

The project implements a sophisticated testing strategy designed for both local development and CI/CD environments:

### Test Categories

- **Unit Tests** (`src/*/tests/`): Fast, isolated tests with mocked dependencies
- **Integration Tests** (`tests/integration/`): Tests that verify component interactions
- **End-to-End Tests** (`tests/e2e/`): Full application workflow tests
- **CircleCI Tests**: CI/CD-compatible tests that handle missing credentials gracefully
- **Local Credentials Tests**: Tests that require Dropbox credentials in a local `.env` file

### Test Markers

The project uses pytest markers to categorize tests:
```bash
@pytest.mark.unit              # Fast unit tests
@pytest.mark.integration       # Integration tests
@pytest.mark.e2e              # End-to-end tests
@pytest.mark.circleci         # CI/CD compatible
@pytest.mark.local_credentials # Requires local auth files
```

### Authentication in Tests

The testing infrastructure handles different authentication scenarios:
- **Unit Tests**: Never touch Dropbox; OAuth, token storage, and HTTP are mocked
- **Local Development**: Uses `.env` plus the token file written by `uv run python -m dropbox_client_impl login`
- **CI/CD Environment**: Uses the `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, and `DROPBOX_REFRESH_TOKEN` environment variables
- **Missing Credentials**: Real-Dropbox tests are skipped; nothing prompts or hangs

## Continuous Integration

The project includes a comprehensive CircleCI configuration (`.circleci/config.yml`) with:

- **All Branches**: Linting, format check, type checking, unit tests, and CI-compatible tests
- **`main` and `dev`**: Additional integration tests with real Dropbox API calls
- **Artifacts**: Coverage reports, test results, and build summaries

See `docs/circleci-setup.md` for detailed CI/CD setup instructions.

## Development Workflow

### Quick Start
1. **Install dependencies**: `uv sync --all-packages --extra dev`
2. **Run tests**: `uv run pytest tests/ -v` or `uv run pytest src/ tests/ -m "not local_credentials" -v`
3. **Check code quality**: `uv run ruff check . && uv run ruff format --check .`
4. **Fix formatting**: `uv run ruff format .`
5. **View documentation**: `uv run mkdocs serve`

### Best Practices
- Run unit tests (`uv run pytest src/`) during development for fast feedback
- Use integration tests (`uv run pytest -m integration`) to verify component interactions
- Run full test suite (`uv run pytest`) before pushing to ensure CI compatibility
- The CircleCI pipeline provides automated validation on every push