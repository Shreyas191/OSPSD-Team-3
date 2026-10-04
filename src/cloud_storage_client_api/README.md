# Cloud Storage Client API

## Overview
`cloud_storage_client_api` defines the `Client` and `File` abstract base classes that every cloud storage provider must implement. The package contains the abstractions, a factory hook, and no concrete logic.

## Purpose
- Document the operations available to consumers (create, read, update, delete).
- Provide a single factory (`get_client`) that implementations can override.
- Keep provider-specific types (e.g. Dropbox SDK objects) out of consumer code.

## Architecture

### `Client` operations
All paths are absolute within the user's storage and start with `/` (e.g. `/Documents/report.pdf`).

| Area   | Method | Returns |
|--------|--------|---------|
| Create | `upload_file(local_path, remote_path, *, overwrite=False)` | `File` |
| Create | `create_folder(remote_path)` | `File` |
| Create | `copy_file(from_path, to_path)` | `File` |
| Create | `copy_folder(from_path, to_path)` | `File` |
| Read   | `download_file(remote_path, local_path)` | `File` |
| Read   | `get_metadata(remote_path)` | `File` |
| Read   | `list_folder(remote_path="")` | `Iterator[File]` |
| Read   | `search(query, max_results=10)` | `Iterator[File]` |
| Update | `rename(remote_path, new_name)` | `File` |
| Update | `move(from_path, to_path)` | `File` |
| Delete | `delete(remote_path)` | `bool` |

### `File` properties
`id`, `name`, `path`, `is_folder`, `size` (bytes, `None` for folders), `modified` (`datetime`, `None` for folders).

### API Integration
```python
from cloud_storage_client_api import Client, get_client

client: Client = get_client()
for entry in client.list_folder("/Documents"):
    print(entry.path, entry.size)
```

### Dependency Injection
Implementation packages (for example `dropbox_client_impl`) replace the factory at import time:
```python
import dropbox_client_impl  # rebinds cloud_storage_client_api.get_client
```

## Testing
```bash
uv run pytest src/cloud_storage_client_api/tests/
```
