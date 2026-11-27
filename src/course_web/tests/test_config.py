"""Settings: the checks that stop a misconfigured interface at startup."""

import pytest

from course_web.config import ConfigurationError, Settings


def test_no_settings_mean_no_api_and_secure_cookies() -> None:
    """No settings mean no API service and Secure cookies."""
    assert Settings.from_env({}) == Settings(
        api_base_url=None,
        secure_cookies=True,
        public_url=None,
    )


def test_the_api_url_is_kept_without_a_trailing_slash() -> None:
    """The API URL is kept without a trailing slash."""
    settings = Settings.from_env({"API_BASE_URL": "https://api.example/ "})

    assert settings.api_base_url == "https://api.example"


def test_an_api_url_that_is_not_http_is_refused_and_named() -> None:
    """An API URL that isn't http(s) is refused, and the message names the setting."""
    with pytest.raises(ConfigurationError, match="API_BASE_URL"):
        Settings.from_env({"API_BASE_URL": "api.example"})


def test_insecure_cookies_work_on_a_laptop() -> None:
    """Insecure cookies work on a laptop, for http://localhost."""
    assert Settings.from_env({"INSECURE_COOKIES": "enabled"}).secure_cookies is False


def test_insecure_cookies_are_refused_on_cloud_run() -> None:
    """Insecure cookies are refused on Cloud Run."""
    with pytest.raises(ConfigurationError, match="INSECURE_COOKIES"):
        Settings.from_env({"INSECURE_COOKIES": "enabled", "K_SERVICE": "course-web-test"})


def test_the_public_url_is_kept_without_a_trailing_slash() -> None:
    """The public address is kept without a trailing slash."""
    settings = Settings.from_env({"PUBLIC_URL": "https://web.example/"})

    assert settings.public_url == "https://web.example"


@pytest.mark.parametrize("value", ["web.example", "https://web.example/app", "https://"])
def test_a_public_url_that_is_not_just_an_address_is_refused(value: str) -> None:
    """A public address must be a scheme and host, with no path; the message names it."""
    with pytest.raises(ConfigurationError, match="PUBLIC_URL"):
        Settings.from_env({"PUBLIC_URL": value})
