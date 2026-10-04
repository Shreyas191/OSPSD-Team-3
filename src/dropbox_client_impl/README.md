# Dropbox Client Implementation

## Overview
`dropbox_client_impl` ships a concrete `cloud_storage_client_api.Client` backed by the official [Dropbox Python SDK](https://github.com/dropbox/dropbox-sdk-python). It handles authentication, calls the Dropbox API, and returns `DropboxFile` objects that implement `cloud_storage_client_api.File`.

## Authentication
The client reads a long-lived OAuth2 refresh token from environment variables (or a local `.env` file, see `.env.example`):

| Variable | Description |
|----------|-------------|
| `DROPBOX_APP_KEY` | App key from the [Dropbox App Console](https://www.dropbox.com/developers/apps) |
| `DROPBOX_APP_SECRET` | App secret from the App Console |
| `DROPBOX_REFRESH_TOKEN` | Refresh token for the account the app acts on |

If any are missing, `DropboxClient()` raises `RuntimeError` naming the missing variables. Tests can skip authentication by passing a pre-built SDK instance: `DropboxClient(dbx=mock)`.

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
