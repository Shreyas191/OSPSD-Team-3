"""Agent service settings, read once from the environment, and the model they select."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Self

from course_agent_service.model import ModelClient, ScriptedModel

ModelProvider = Literal["gemini", "scripted", "disabled"]
MODEL_PROVIDERS: tuple[ModelProvider, ...] = ("gemini", "scripted", "disabled")
# A fixed version, not the moving "-latest" alias, so evaluation runs compare like with like.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEMO_SERVICE = "course-agent-demo"


class ConfigurationError(RuntimeError):
    """A setting is missing or invalid. The message names settings, never their values."""


@dataclass(frozen=True)
class Settings:
    """The agent service's configuration."""

    model_provider: ModelProvider
    gemini_api_key: str
    gemini_model: str
    api_base_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read and check the settings. Raises ``ConfigurationError`` naming what's wrong."""
        env = os.environ if env is None else env
        provider = env.get("MODEL_PROVIDER", "disabled")
        if provider not in MODEL_PROVIDERS:
            msg = f"MODEL_PROVIDER must be one of {', '.join(MODEL_PROVIDERS)}"
            raise ConfigurationError(msg)
        if provider == "scripted" and env.get("K_SERVICE") == DEMO_SERVICE:
            msg = "MODEL_PROVIDER=scripted is for tests and is refused on the demo service"
            raise ConfigurationError(msg)
        key = env.get("GEMINI_API_KEY", "")
        if provider == "gemini" and not key:
            msg = "MODEL_PROVIDER=gemini needs GEMINI_API_KEY"
            raise ConfigurationError(msg)
        return cls(
            model_provider=provider,  # type: ignore[arg-type]  # checked against MODEL_PROVIDERS
            gemini_api_key=key,
            gemini_model=env.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            api_base_url=env.get("API_BASE_URL", ""),
        )


def build_model(settings: Settings) -> ModelClient | None:
    """Build the configured model, or return ``None`` when the model is disabled."""
    if settings.model_provider == "gemini":
        # Imported here so the Gemini SDK loads only when this provider is chosen.
        from course_agent_service.gemini import GeminiModel  # noqa: PLC0415

        return GeminiModel(api_key=settings.gemini_api_key, model_name=settings.gemini_model)
    if settings.model_provider == "scripted":
        return ScriptedModel([])
    return None
