from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"
    model: str = ""

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            provider=os.getenv("AI_PROVIDER", "mock").lower(),
            model=os.getenv("AI_MODEL", ""),
        )
