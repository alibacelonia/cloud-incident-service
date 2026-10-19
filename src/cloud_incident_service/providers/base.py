from __future__ import annotations

from typing import Protocol


class AIProvider(Protocol):
    async def generate(self, prompt: str) -> str:
        """Generate text for an already-rendered prompt."""


class ProviderConfigurationError(RuntimeError):
    """Raised when an explicitly selected provider cannot be configured."""
