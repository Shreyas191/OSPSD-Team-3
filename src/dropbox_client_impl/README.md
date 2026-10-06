# Dropbox Client Implementation

## Overview
`dropbox_client_impl` ships a concrete `cloud_storage_client_api.Client` backed by the official [Dropbox Python SDK](https://github.com/dropbox/dropbox-sdk-python). It handles authentication, calls the Dropbox API, and returns `DropboxFile` objects that implement `cloud_storage_client_api.File`.

## Authentication
All credential handling lives in `auth.py`. For now it reads a manually generated access token from `DROPBOX_ACCESS_TOKEN` (environment variable or local `.env` file, see `.env.example`). OAuth will later replace this inside `auth.py` only, so **CRUD code must never read the token from the environment itself**.

| Function | Use it for |
|----------|------------|
| `get_dropbox_client()` | Authenticated `dropbox.Dropbox` SDK instance (what `DropboxClient.dbx` uses) |
| `dropbox_request(endpoint, payload)` | Raw authenticated POST to an RPC endpoint, e.g. `dropbox_request("/files/get_metadata", {"path": "/a.txt"})` |
| `get_current_account()` | Returns the token's account; a quick auth check |

Errors from `dropbox_request` are all subclasses of `DropboxError`:
`DropboxAuthError` (missing, invalid, or expired token), `DropboxAPIError` (other Dropbox error responses, has `.status_code`), and `DropboxConnectionError` (network failures and timeouts). SDK calls on `client.dbx` raise the SDK's own exceptions (`dropbox.exceptions.AuthError`, `ApiError`).

Check that your token works:
```bash
uv run python -m dropbox_client_impl
# Dropbox authentication OK: Jane Doe <jane@example.com>
```

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

`rename` and `move` are implemented: both call `files_move_v2` with `autorename=False`, and translate Dropbox's `not_found` and `conflict` errors into `FileNotFoundError` and `FileExistsError`. The remaining methods are stubs that raise `NotImplementedError`. Implement yours on a `<name>-<feature>` branch, wrap SDK metadata with `DropboxFile(metadata)`, and add unit tests that mock `client.dbx`.

## Testing
```bash
uv run pytest src/dropbox_client_impl/tests/
```
