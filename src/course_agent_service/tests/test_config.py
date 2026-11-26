"""Settings: which model runs, and the checks that stop a misconfigured service at startup."""

import pytest
from fastapi.testclient import TestClient

from course_agent_service.app import app, check_configuration
from course_agent_service.config import (
    DEFAULT_GEMINI_MODEL,
    ConfigurationError,
    Settings,
    build_model,
)
from course_agent_service.gemini import GeminiModel
from course_agent_service.model import ScriptedModel


def test_the_model_is_disabled_by_default() -> None:
    """The model is disabled by default."""
    settings = Settings.from_env({})

    assert settings.model_provider == "disabled"
    assert settings.gemini_model == DEFAULT_GEMINI_MODEL
    assert build_model(settings) is None


def test_gemini_with_a_key_builds_the_gemini_model() -> None:
    """Gemini with a key builds the gemini model."""
    settings = Settings.from_env(
        {"MODEL_PROVIDER": "gemini", "GEMINI_API_KEY": "k", "GEMINI_MODEL": "gemini-x"},
    )

    model = build_model(settings)

    assert isinstance(model, GeminiModel)
    assert model.model_name == "gemini-x"


def test_gemini_without_a_key_is_refused_and_the_message_names_the_setting() -> None:
    """Gemini without a key is refused and the message names the setting."""
    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        Settings.from_env({"MODEL_PROVIDER": "gemini"})


def test_an_unknown_provider_is_refused() -> None:
    """An unknown provider is refused."""
    with pytest.raises(ConfigurationError, match="MODEL_PROVIDER"):
        Settings.from_env({"MODEL_PROVIDER": "gpt"})


def test_the_scripted_model_builds_for_tests() -> None:
    """The scripted model builds for tests."""
    assert isinstance(build_model(Settings.from_env({"MODEL_PROVIDER": "scripted"})), ScriptedModel)


def test_the_scripted_model_is_refused_on_the_demo_service() -> None:
    """The scripted model is refused on the demo service."""
    with pytest.raises(ConfigurationError, match="demo"):
        Settings.from_env({"MODEL_PROVIDER": "scripted", "K_SERVICE": "course-agent-demo"})


def test_the_api_base_url_is_read() -> None:
    """The api base url is read."""
    assert (
        Settings.from_env({"API_BASE_URL": "https://api.example"}).api_base_url
        == "https://api.example"
    )


def test_startup_check_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup check reads the process environment."""
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ConfigurationError):
        check_configuration()


def test_health_answers_without_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Health answers without a model."""
    monkeypatch.setenv("RELEASE", "abc1234")

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "release": "abc1234"}
