from __future__ import annotations

from cloud_incident_service.config import Settings
from cloud_incident_service.providers.base import ProviderConfigurationError
from cloud_incident_service.providers.model_client import ModelClient


def create_model_client(settings: Settings | None = None) -> ModelClient | None:
    """The configured live model client, or None for AI_PROVIDER=mock (no network)."""
    selected = settings or Settings.from_environment()
    if selected.provider == "mock":
        return None
    if selected.provider == "bedrock":
        from cloud_incident_service.providers.bedrock import BedrockModelClient

        return BedrockModelClient(selected.model, selected.aws_region)
    if selected.provider == "anthropic":
        from cloud_incident_service.providers.anthropic import AnthropicModelClient

        return AnthropicModelClient(selected.model, selected.anthropic_api_key)
    raise ProviderConfigurationError(f"AI_PROVIDER: unsupported provider {selected.provider!r}")
