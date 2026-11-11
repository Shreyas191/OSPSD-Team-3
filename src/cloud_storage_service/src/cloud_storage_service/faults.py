"""Fault injection: make the storage provider slow or failing on purpose, in test deployments only.

Off unless ``FAULT_INJECTION`` is set. When it is, :func:`with_faults` wraps the storage
client in a :class:`FaultInjectingClient`, which fails the way a real provider does: it
raises the same built-in exceptions the ``Client`` contract documents, so the routes, the
error handlers and the telemetry see exactly what they would see if Dropbox failed.

``FAULT_INJECTION`` takes one fault:

- ``latency=<ms>``: wait ``ms``, then call the provider. The caller gets the normal answer,
  later.
- ``unavailable``: raise ``ConnectionError`` without calling the provider (``503
  provider_unavailable``).
- ``error``: raise ``OSError`` without calling the provider (``502 provider_error``).
- ``timeout=<ms>``: wait ``ms``, then raise ``ConnectionError`` (``503``, after the wait).
- ``lost_response``: call the provider, **then** raise ``ConnectionError`` (``503``, but the
  change was made).

``lost_response`` is the uncertain write the contract warns about: a write that took effect
although its reply never arrived.

Two optional settings narrow the fault: ``FAULT_OPERATIONS`` (comma-separated ``Client``
method names, e.g. ``get_metadata,download_file``; default: all) and ``FAULT_RATE`` (the
fraction of calls that fail, 0 to 1; default 1).

**Never in the demo environment:** if ``FAULT_INJECTION`` is set on the demo service, the
service refuses to start (:func:`with_faults` raises). OpenTofu never sets it.
"""

import os
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from cloud_storage_client_api import Client, File

T = TypeVar("T")

FAULT_ENV_VAR = "FAULT_INJECTION"
OPERATIONS_ENV_VAR = "FAULT_OPERATIONS"
RATE_ENV_VAR = "FAULT_RATE"
DEMO_SERVICE = "cloud-storage-service-demo"

_KINDS_WITH_DELAY = {"latency", "timeout"}
_KINDS = _KINDS_WITH_DELAY | {"unavailable", "error", "lost_response"}


class FaultConfigError(RuntimeError):
    """``FAULT_INJECTION`` is malformed, or set where it must never be."""


@dataclass(frozen=True)
class Fault:
    """One fault: its kind, a delay for latency and timeout, which operations, how often."""

    kind: str
    delay_ms: int = 0
    operations: frozenset[str] = frozenset()  # empty means every operation
    rate: float = 1.0

    @classmethod
    def parse(cls, value: str, operations: str = "", rate: str = "") -> "Fault":
        """Parse ``FAULT_INJECTION`` (``kind`` or ``kind=<ms>``) and its two options."""
        kind, _, delay = value.strip().partition("=")
        if kind not in _KINDS:
            msg = f"{FAULT_ENV_VAR}: unknown fault {kind!r}; use one of {sorted(_KINDS)}"
            raise FaultConfigError(msg)
        if (kind in _KINDS_WITH_DELAY) != bool(delay):
            need = "needs" if kind in _KINDS_WITH_DELAY else "takes no"
            msg = f"{FAULT_ENV_VAR}: {kind!r} {need} =<ms>"
            raise FaultConfigError(msg)
        if delay and (not delay.isdigit() or int(delay) > 60_000):  # noqa: PLR2004
            msg = f"{FAULT_ENV_VAR}: delay must be 0 to 60000 ms, got {delay!r}"
            raise FaultConfigError(msg)
        names = frozenset(n.strip() for n in operations.split(",") if n.strip())
        unknown = names - _OPERATION_NAMES
        if unknown:
            msg = f"{OPERATIONS_ENV_VAR}: unknown operations {sorted(unknown)}"
            raise FaultConfigError(msg)
        try:
            fraction = float(rate) if rate.strip() else 1.0
        except ValueError:
            fraction = -1.0
        if not 0 <= fraction <= 1:
            msg = f"{RATE_ENV_VAR}: must be a number from 0 to 1, got {rate!r}"
            raise FaultConfigError(msg)
        return cls(kind, int(delay or 0), names, fraction)

    def applies_to(self, operation: str) -> bool:
        """Return whether this call of ``operation`` should fail (or be slowed)."""
        if self.operations and operation not in self.operations:
            return False
        return self.rate >= 1 or random.random() < self.rate  # noqa: S311 - not security


def with_faults(client: Client) -> Client:
    """Return ``client``, wrapped in faults if ``FAULT_INJECTION`` asks for them.

    Raises:
        FaultConfigError: If the setting is malformed, or set on the demo service, so a
            mistake stops the service at startup instead of failing requests later.

    """
    value = os.environ.get(FAULT_ENV_VAR, "").strip()
    if not value:
        return client
    if os.environ.get("K_SERVICE") == DEMO_SERVICE:
        msg = f"{FAULT_ENV_VAR} is set on {DEMO_SERVICE}; fault injection is never allowed there"
        raise FaultConfigError(msg)
    fault = Fault.parse(
        value,
        os.environ.get(OPERATIONS_ENV_VAR, ""),
        os.environ.get(RATE_ENV_VAR, ""),
    )
    return FaultInjectingClient(client, fault)


class FaultInjectingClient(Client):
    """A ``Client`` that delegates to another one and injects one kind of fault."""

    def __init__(self, inner: Client, fault: Fault) -> None:
        """Wrap ``inner`` with ``fault``."""
        self.inner = inner
        self.fault = fault

    def _call(self, operation: str, call: Callable[[], T]) -> T:
        if not self.fault.applies_to(operation):
            return call()
        kind = self.fault.kind
        if kind in _KINDS_WITH_DELAY:
            time.sleep(self.fault.delay_ms / 1000)
        if kind == "latency":
            return call()
        if kind == "lost_response":
            call()
            msg = (
                f"Injected fault: the provider's reply to {operation} was lost after it took effect"
            )
            raise ConnectionError(msg)
        if kind == "error":
            msg = f"Injected fault: the provider failed {operation}"
            raise OSError(msg)
        msg = f"Injected fault: the provider is unreachable ({kind}) for {operation}"
        raise ConnectionError(msg)

    def _iterate(self, operation: str, call: Callable[[], Iterator[File]]) -> Iterator[File]:
        # Listing and search are lazy; collect them so the fault applies to the whole call.
        return iter(self._call(operation, lambda: list(call())))

    def upload_file(self, local_path: str, remote_path: str, *, overwrite: bool = False) -> File:
        """Upload, through the fault."""
        return self._call(
            "upload_file",
            lambda: self.inner.upload_file(local_path, remote_path, overwrite=overwrite),
        )

    def replace_file(self, local_path: str, remote_path: str) -> File:
        """Replace, through the fault."""
        return self._call("replace_file", lambda: self.inner.replace_file(local_path, remote_path))

    def create_folder(self, remote_path: str) -> File:
        """Create a folder, through the fault."""
        return self._call("create_folder", lambda: self.inner.create_folder(remote_path))

    def copy(self, from_path: str, to_path: str) -> File:
        """Copy, through the fault."""
        return self._call("copy", lambda: self.inner.copy(from_path, to_path))

    def download_file(self, remote_path: str, local_path: str) -> File:
        """Download, through the fault."""
        return self._call(
            "download_file",
            lambda: self.inner.download_file(remote_path, local_path),
        )

    def get_metadata(self, remote_path: str) -> File:
        """Read metadata, through the fault."""
        return self._call("get_metadata", lambda: self.inner.get_metadata(remote_path))

    def list_folder(self, remote_path: str = "") -> Iterator[File]:
        """List a folder, through the fault."""
        return self._iterate("list_folder", lambda: self.inner.list_folder(remote_path))

    def search(self, query: str, max_results: int = 10) -> Iterator[File]:
        """Search, through the fault."""
        return self._iterate("search", lambda: self.inner.search(query, max_results))

    def rename(self, remote_path: str, new_name: str) -> File:
        """Rename, through the fault."""
        return self._call("rename", lambda: self.inner.rename(remote_path, new_name))

    def move(self, from_path: str, to_path: str) -> File:
        """Move, through the fault."""
        return self._call("move", lambda: self.inner.move(from_path, to_path))

    def check_health(self) -> None:
        """Check the provider, through the fault."""
        self._call("check_health", self.inner.check_health)

    def delete(self, remote_path: str) -> bool:
        """Delete, through the fault."""
        return self._call("delete", lambda: self.inner.delete(remote_path))


_OPERATION_NAMES = frozenset(
    name
    for name, value in vars(Client).items()
    if callable(value) and getattr(value, "__isabstractmethod__", False)
)
