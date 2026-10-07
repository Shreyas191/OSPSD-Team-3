# Dropbox Client Implementation

## Overview
`dropbox_client_impl` ships a concrete `cloud_storage_client_api.Client` backed by the official [Dropbox Python SDK](https://github.com/dropbox/dropbox-sdk-python). It handles authentication, calls the Dropbox API, and returns `DropboxFile` objects that implement `cloud_storage_client_api.File`.

## Authentication (OAuth 2.0)
All credential handling lives in `auth.py`. Each teammate authorizes **their own** Dropbox account once with OAuth; nobody generates or copies access tokens by hand. **CRUD code must never read credentials from the environment itself**; it only uses `DropboxClient.dbx`.

### 1. Create / configure the Dropbox app (once per team)
1. Go to the [Dropbox App Console](https://www.dropbox.com/developers/apps) and click **Create app**. Choose **Scoped access** and **App folder** (or **Full Dropbox**), then name the app.
2. On the **Permissions** tab, enable the scopes the client needs (`files.metadata.read`, `files.metadata.write`, `files.content.read`, `files.content.write`; `account_info.read` is on by default), then click **Submit**.
3. On the **Settings** tab:
   - Under **OAuth 2 → Redirect URIs**, add exactly `http://localhost:8080/oauth/callback` and click **Add**.
   - Note the **App key** and **App secret**. Share them with teammates privately (not through git).

While the app is in "Development" status, Dropbox lets up to 500 users link it, which is plenty for the team. If you change scopes later, everyone must run `login` again.

### 2. Configure your environment
```bash
cp .env.example .env
```
Then edit `.env`:

| Variable | Required | Meaning |
|----------|----------|---------|
| `DROPBOX_APP_KEY` | yes | App key from the Settings tab |
| `DROPBOX_APP_SECRET` | yes | App secret from the Settings tab |
| `DROPBOX_REDIRECT_URI` | for `login` | Must match a registered redirect URI exactly, e.g. `http://localhost:8080/oauth/callback`. Must be a local `http://localhost:<port>/...` or `http://127.0.0.1:<port>/...` URL |
| `DROPBOX_TOKEN_FILE` | no | Where `login` stores credentials (default `.dropbox_token.json` in the current directory) |
| `DROPBOX_REFRESH_TOKEN` | no | CI only: a refresh token supplied directly instead of the token file |
| `DROPBOX_ACCESS_TOKEN` | no | Development fallback: a manually generated short-lived token, used only if no refresh token is available |

### 3. Authorize your account
Run this from the project root:
```bash
uv run python -m dropbox_client_impl login
```
1. The command prints a `https://www.dropbox.com/oauth2/authorize?...` URL and tries to open it in your browser. If no browser opens, copy the URL manually.
2. Sign in to Dropbox and click **Allow**.
3. Dropbox redirects to `http://localhost:8080/oauth/callback`, where the command is listening. The page says *"Dropbox authorization received. You can close this tab..."*
4. The terminal confirms that it saved your credentials, then checks them: `Dropbox authentication OK: Jane Doe <jane@example.com>`.

If you click **Cancel**, or don't finish within 5 minutes, the command fails cleanly and saves nothing.

### Where credentials are stored
`login` requests an *offline* token, which gives a long-lived **refresh token**. Only the refresh token (plus your account ID) is saved, in `.dropbox_token.json` (or `DROPBOX_TOKEN_FILE`), with file mode `600`. The file is listed in `.gitignore`. `get_dropbox_client()` passes the refresh token, app key, and app secret to the Dropbox SDK, which fetches and renews short-lived access tokens automatically. Tokens are never printed or logged.

Credentials are resolved in this order: `DROPBOX_REFRESH_TOKEN`, then the token file, then `DROPBOX_ACCESS_TOKEN`.

### Check, revoke, and re-authorize
```bash
uv run python -m dropbox_client_impl          # check: prints the authenticated account
uv run python -m dropbox_client_impl logout   # revoke the saved token with Dropbox and delete the file
uv run python -m dropbox_client_impl login    # authorize again (also fixes "revoked or expired" errors)
```
You can also revoke access from Dropbox itself: go to **Settings → Connected apps** on dropbox.com and remove the app. After that, the saved token stops working and you need to run `login` again.

**Never commit `.env` or `.dropbox_token.json`.** Both contain secrets and are gitignored.

### API
| Function | Use it for |
|----------|------------|
| `get_dropbox_client()` | Authenticated `dropbox.Dropbox` SDK instance (what `DropboxClient.dbx` uses) |
| `dropbox_request(endpoint, payload)` | Raw authenticated POST to an RPC endpoint, e.g. `dropbox_request("/files/get_metadata", {"path": "/a.txt"})` |
| `get_current_account()` | Returns the authenticated account; a quick auth check |

Errors from `auth.py` are all subclasses of `DropboxError`:
`DropboxAuthError` (not logged in, missing configuration, or revoked/expired credentials), `DropboxAPIError` (other Dropbox error responses, has `.status_code`), and `DropboxConnectionError` (network failures and timeouts). SDK calls on `client.dbx` raise the SDK's own exceptions (`dropbox.exceptions.AuthError`, `ApiError`).

Tests can skip authentication by passing a pre-built SDK instance: `DropboxClient(dbx=mock)`.

### Dependency Injection
```python
import dropbox_client_impl  # rebinds the factory

from cloud_storage_client_api import get_client
client = get_client()  # -> DropboxClient
```

## Method ownership and Dropbox endpoints

| Method | Owner | Dropbox SDK call |
|--------|-------|------------------|
| `upload_file` | Zesan | `files_upload` (or `files_upload_session_*` for large files) |
| `create_folder` | Zesan | `files_create_folder_v2` |
| `copy_file` | Zesan | `files_copy_v2` |
| `copy_folder` | Zesan | `files_copy_v2` |
| `download_file` | Jing | `files_download_to_file` |
| `get_metadata` | Jing | `files_get_metadata` |
| `list_folder` | Jing | `files_list_folder` / `files_list_folder_continue` |
| `search` | Jing | `files_search_v2` |
| `rename` | Shreyas | `files_move_v2` (same folder, new name) |
| `move` | Shreyas | `files_move_v2` |
| `delete` | John | `files_delete_v2` |

Each method is currently a stub that raises `NotImplementedError`. Implement yours on a `<name>-<feature>` branch, wrap SDK metadata with `DropboxFile(metadata)`, and add unit tests that mock `client.dbx`.

## Testing
```bash
uv run pytest src/dropbox_client_impl/tests/
```
