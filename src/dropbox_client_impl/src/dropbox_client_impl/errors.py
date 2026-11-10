"""Translate Dropbox SDK failures into the built-in exceptions the ``Client`` contract documents.

Every failed call is translated in the same three steps:

1. Failures that mean the same for every call: the SDK's own argument check becomes
   ``malformed``, and rate limiting or a network error becomes ``unavailable``.
2. Otherwise the operation's reason reader turns Dropbox's error reason into an outcome
   such as ``not_found`` or ``conflict``. Anything it does not recognise is ``failed``.
3. :data:`_OUTCOMES` gives each outcome its exception class and message.

Only provider failures (``ConnectionError`` and ``OSError``) carry ``detail``: the Dropbox
error tag or the SDK exception's class name, neither of which holds credentials. The
service never shows those messages to callers.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import dropbox
import requests
from dropbox import files, stone_validators

# Every failure an SDK call can raise: Dropbox API and HTTP errors, the SDK's local
# validation of arguments (e.g. a path without a leading "/"), and network errors.
DROPBOX_FAILURES = (
    dropbox.exceptions.DropboxException,
    stone_validators.ValidationError,
    requests.exceptions.RequestException,
)

Outcome = Literal[
    "not_found",
    "not_file",
    "not_folder",
    "conflict",
    "stale",
    "malformed",
    "too_large",
    "nested",
    "denied",
    "unavailable",
    "cursor_reset",
    "failed",
]

# ``source`` is the path the caller named, ``destination`` the path written to (the same
# path unless the operation has a destination), and ``subject`` both together.
_OUTCOMES: dict[Outcome, tuple[type[Exception], str]] = {
    "not_found": (FileNotFoundError, "No file or folder at {source}"),
    "not_file": (IsADirectoryError, "Path is a folder: {source}"),
    "not_folder": (ValueError, "Path is a file: {source}"),
    "conflict": (FileExistsError, "Something already exists at {destination}"),
    "stale": (
        FileExistsError,
        "{destination} changed while it was being replaced; read it again and retry",
    ),
    "malformed": (ValueError, "Dropbox rejected {subject} as invalid"),
    "too_large": (
        ValueError,
        "The file for {destination} is too large to upload in one request "
        "(Dropbox's limit is 150 MB)",
    ),
    "nested": (ValueError, "Dropbox refused {action} {subject}: a folder cannot go inside itself"),
    "denied": (PermissionError, "Dropbox does not allow {action} {subject}"),
    "unavailable": (
        ConnectionError,
        "Dropbox is unreachable or busy while {action} {subject} ({detail})",
    ),
    "cursor_reset": (OSError, "Dropbox invalidated the cursor while listing {source}"),
    "failed": (OSError, "Dropbox failed {action} {subject} ({detail})"),
}

ReasonReader = Callable[[object], Outcome | None]


def _no_specific_reason(_reason: object) -> Outcome | None:
    return None


@dataclass(frozen=True)
class Operation:
    """How to describe one Dropbox call in messages, and how to read its error reason."""

    action: str
    read_reason: ReasonReader = _no_specific_reason
    describe: Callable[[str], str] = repr


def translate(
    error: Exception,
    operation: Operation,
    path: str,
    to_path: str | None = None,
) -> Exception:
    """Return the contract exception for a failed SDK call.

    Args:
        error: The exception the SDK raised (one of :data:`DROPBOX_FAILURES`).
        operation: The call that failed, for example :data:`MOVE`.
        path: The path (or, for search, the query) the caller named.
        to_path: The destination, for operations that have one (move and copy).

    """
    reason = error.error if isinstance(error, dropbox.exceptions.ApiError) else None
    outcome = _sdk_outcome(error) or operation.read_reason(reason) or "failed"
    kind, template = _OUTCOMES[outcome]
    source = operation.describe(path)
    destination = source if to_path is None else repr(to_path)
    subject = source if to_path is None else f"{source} to {destination}"
    detail = reason if reason is not None else type(error).__name__
    return kind(
        template.format(
            action=operation.action,
            source=source,
            destination=destination,
            subject=subject,
            detail=detail,
        ),
    )


def _sdk_outcome(error: Exception) -> Outcome | None:
    """Return the outcome for failures that mean the same for every call, else ``None``."""
    if isinstance(error, stone_validators.ValidationError):
        return "malformed"
    if isinstance(error, dropbox.exceptions.RateLimitError | requests.exceptions.RequestException):
        return "unavailable"
    return None


# ----- Reasons shared by several calls -----


def _lookup_outcome(lookup: files.LookupError) -> Outcome | None:
    """Return the outcome for a failed path lookup, or ``None`` if none fits."""
    if lookup.is_not_found():
        return "not_found"
    if lookup.is_malformed_path():
        return "malformed"
    if lookup.is_restricted_content() or lookup.is_locked():
        return "denied"
    return None


def _write_outcome(write: files.WriteError) -> Outcome | None:
    """Return the outcome for a failed write, or ``None`` if none fits (e.g. a full account)."""
    if write.is_malformed_path() or write.is_disallowed_name():
        return "malformed"
    if (
        write.is_no_write_permission()
        or write.is_team_folder()
        or write.is_access_restricted()
        or write.is_operation_suppressed()
    ):
        return "denied"
    if write.is_too_many_write_operations():
        return "unavailable"
    return None


def _destination_outcome(write: files.WriteError) -> Outcome | None:
    """Like :func:`_write_outcome`, but an occupied destination is a conflict."""
    return "conflict" if write.is_conflict() else _write_outcome(write)


# ----- One reason reader per Dropbox call -----


def _download_reason(reason: object) -> Outcome | None:
    if isinstance(reason, files.DownloadError) and reason.is_path():
        lookup = reason.get_path()
        return "not_file" if lookup.is_not_file() else _lookup_outcome(lookup)
    return None


def _metadata_reason(reason: object) -> Outcome | None:
    if isinstance(reason, files.GetMetadataError) and reason.is_path():
        return _lookup_outcome(reason.get_path())
    return None


def _list_reason(reason: object) -> Outcome | None:
    if isinstance(reason, files.ListFolderError) and reason.is_path():
        lookup = reason.get_path()
        return "not_folder" if lookup.is_not_folder() else _lookup_outcome(lookup)
    return None


def _list_continue_reason(reason: object) -> Outcome | None:
    if isinstance(reason, files.ListFolderContinueError):
        if reason.is_path() and reason.get_path().is_not_found():
            return "not_found"
        if reason.is_reset():
            return "cursor_reset"
    return None


def _write_reason(reason: object) -> Outcome | None:
    """Read a failed ``files_upload`` or ``files_create_folder_v2``."""
    if isinstance(reason, files.UploadError):
        if reason.is_payload_too_large():
            return "too_large"
        if reason.is_path():
            return _destination_outcome(reason.get_path().reason)
    if isinstance(reason, files.CreateFolderError) and reason.is_path():
        return _destination_outcome(reason.get_path())
    return None


def _replace_reason(reason: object) -> Outcome | None:
    """Read a failed revision-checked ``files_upload`` (``WriteMode.update``).

    With ``autorename=False``, Dropbox reports a revision that no longer matches the file
    as a write conflict: someone changed the file after we read its revision.
    """
    outcome = _write_reason(reason)
    return "stale" if outcome == "conflict" else outcome


def _relocation_reason(reason: object) -> Outcome | None:  # noqa: PLR0911
    """Read a failed ``files_move_v2`` or ``files_copy_v2``."""
    if not isinstance(reason, files.RelocationError):
        return None
    if reason.is_from_lookup():
        return _lookup_outcome(reason.get_from_lookup())
    if reason.is_from_write():
        return _write_outcome(reason.get_from_write())
    if reason.is_to():
        return _destination_outcome(reason.get_to())
    # ``move`` and ``copy`` reject nested destinations before calling Dropbox, so this means
    # the destination is the source itself: it is occupied.
    if reason.is_duplicated_or_nested_paths():
        return "conflict"
    if reason.is_cant_move_folder_into_itself():
        return "nested"
    if (
        reason.is_cant_copy_shared_folder()
        or reason.is_cant_nest_shared_folder()
        or reason.is_cant_move_shared_folder()
        or reason.is_cant_move_into_vault()
        or reason.is_cant_move_into_family()
        or reason.is_cant_transfer_ownership()
    ):
        return "denied"
    # Quota, too_many_files, internal_error and unknown tags.
    return None


def _delete_reason(reason: object) -> Outcome | None:
    if not isinstance(reason, files.DeleteError):
        return None
    if reason.is_too_many_write_operations():
        return "unavailable"
    if reason.is_path_lookup():
        return _lookup_outcome(reason.get_path_lookup())
    if reason.is_path_write():
        return _write_outcome(reason.get_path_write())
    # too_many_files (the folder is too large to delete in one call) and unknown tags.
    return None


_QUERY_PREVIEW_LENGTH = 50


def _describe_query(query: str) -> str:
    """Quote ``query``, cut to its first 50 characters so a long query is not echoed in full."""
    if len(query) <= _QUERY_PREVIEW_LENGTH:
        return repr(query)
    return f"{query[:_QUERY_PREVIEW_LENGTH]!r}... ({len(query)} characters)"


DOWNLOAD = Operation("reading", _download_reason)
METADATA = Operation("reading", _metadata_reason)
LIST = Operation("listing", _list_reason)
LIST_CONTINUE = Operation("listing", _list_continue_reason)
SEARCH = Operation("searching for", describe=_describe_query)
UPLOAD = Operation("uploading to", _write_reason)
REPLACE = Operation("replacing", _replace_reason)
CREATE_FOLDER = Operation("creating a folder at", _write_reason)
MOVE = Operation("moving", _relocation_reason)
COPY = Operation("copying", _relocation_reason)
DELETE = Operation("deleting", _delete_reason)
HEALTH = Operation("checking", describe=lambda _: "the Dropbox account")
