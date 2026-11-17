"""The error raised when the service's configuration is missing or unusable."""


class ConfigurationError(RuntimeError):
    """Required configuration, such as a provider credential, is missing or unusable.

    The message names the settings involved and never contains their values, so it is safe
    to log.
    """
