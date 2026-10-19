from __future__ import annotations

from cloud_incident_service.config import ConfigurationError


class ProviderConfigurationError(ConfigurationError):
    """Raised when an explicitly selected provider cannot be configured."""

    def __init__(self, message: str) -> None:
        super().__init__([message])


class ProviderInvocationError(RuntimeError):
    """Raised when a provider call fails. Carries a stable code and a safe message only.

    `unavailable` is true for failures the caller may retry once the cause is fixed
    (credentials, access, throttling, connectivity), and false when the provider
    rejected the request or returned an unexpected response.
    """

    def __init__(self, code: str, message: str, *, unavailable: bool) -> None:
        self.code = code
        self.message = message
        self.unavailable = unavailable
        super().__init__(f"{code}: {message}")
