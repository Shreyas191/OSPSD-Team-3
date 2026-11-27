"""The interface's settings, read once at startup.

- ``API_BASE_URL``: the API service's URL. Without it, the start page says sign-in isn't open.
- ``PUBLIC_URL``: the interface's one public address (scheme and host, no path). Page requests
  on any other address are sent there, so cookies and the sign-in callback share one host.
  Unset on a laptop.
- ``INSECURE_COOKIES``: ``enabled`` drops the cookies' ``Secure`` flag, for ``http://`` on a
  laptop. Refused on Cloud Run.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """A setting is invalid; the message names it."""


@dataclass(frozen=True)
class Settings:
    """What the interface needs to know about its environment."""

    api_base_url: str | None = None
    secure_cookies: bool = True
    public_url: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read the settings from the environment (or ``env``, in tests)."""
        env = os.environ if env is None else env
        api = env.get("API_BASE_URL", "").strip()
        if api and not api.startswith(("https://", "http://")):
            msg = "API_BASE_URL must be an http:// or https:// URL"
            raise ConfigurationError(msg)
        insecure = env.get("INSECURE_COOKIES", "") == "enabled"
        if insecure and env.get("K_SERVICE"):
            # Cloud Run sets K_SERVICE: a deployed interface always uses Secure cookies.
            msg = "INSECURE_COOKIES is for local development only, not a deployed service"
            raise ConfigurationError(msg)
        public = env.get("PUBLIC_URL", "").strip().rstrip("/")
        parts = urlsplit(public)
        just_an_address = parts.scheme in {"https", "http"} and parts.netloc and not parts.path
        if public and not (just_an_address and not parts.query):
            msg = "PUBLIC_URL must be an http:// or https:// address with no path"
            raise ConfigurationError(msg)
        return cls(
            api_base_url=api.rstrip("/") or None,
            secure_cookies=not insecure,
            public_url=public or None,
        )
