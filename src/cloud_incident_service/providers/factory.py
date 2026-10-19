from __future__ import annotations

from cloud_incident_service.config import Settings
from cloud_incident_service.providers.base import AIProvider, ProviderConfigurationError
from cloud_incident_service.providers.mock import MockProvider


def create_provider(settings: Settings | None = None) -> AIProvider:
    selected = settings or Settings.from_environment()
    if selected.provider == "mock":
        return MockProvider()
    if selected.provider == "bedrock":
        from cloud_incident_service.providers.bedrock import BedrockProvider

        return BedrockProvider(selected.model, selected.aws_region)
    raise ProviderConfigurationError(f"AI_PROVIDER: unsupported provider {selected.provider!r}")
