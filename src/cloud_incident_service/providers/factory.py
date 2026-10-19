from __future__ import annotations

from cloud_incident_service.config import Settings
from cloud_incident_service.providers.base import AIProvider, ProviderConfigurationError
from cloud_incident_service.providers.mock import MockProvider


class BedrockProvider:
    def __init__(self, model: str) -> None:
        if not model:
            raise ProviderConfigurationError("AI_MODEL is required for AI_PROVIDER=bedrock")
        import boto3

        self._client = boto3.client("bedrock-runtime")
        self._model = model

    async def generate(self, prompt: str) -> str:
        response = self._client.converse(
            modelId=self._model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
        )
        return response["output"]["message"]["content"][0]["text"]


def create_provider(settings: Settings | None = None) -> AIProvider:
    selected = settings or Settings.from_environment()
    if selected.provider == "mock":
        return MockProvider()
    if selected.provider == "bedrock":
        return BedrockProvider(selected.model)
    raise ProviderConfigurationError(f"Unsupported AI provider: {selected.provider}")
