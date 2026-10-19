from __future__ import annotations


class MockProvider:
    """Deterministic provider used for local development and tests."""

    async def generate(self, prompt: str) -> str:
        compact = " ".join(prompt.split())
        return f"mock:{compact[:200]}"
