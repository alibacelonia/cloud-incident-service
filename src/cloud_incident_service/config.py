from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Maps settings fields to the environment variables they are read from.
ENVIRONMENT_VARIABLES: dict[str, str] = {
    "provider": "AI_PROVIDER",
    "model": "AI_MODEL",
    "aws_region": "AWS_REGION",
    "trace_dir": "TRACE_DIR",
}


class ConfigurationError(RuntimeError):
    """Raised when settings are invalid. Messages name variables, never their values."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Invalid configuration: " + "; ".join(errors))


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["mock", "bedrock"] = "mock"
    model: str = Field(default="", max_length=256, pattern=r"^\S*$")
    aws_region: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[a-z]+)+-\d+$")
    # D-3: traces are written only when a directory is configured.
    trace_dir: Path | None = None

    @field_validator("trace_dir", mode="before")
    @classmethod
    def _check_trace_dir(cls, value: object) -> object:
        if isinstance(value, str) and ("\x00" in value or len(value) > 1_024):
            raise ValueError("must be a directory path of at most 1024 characters")
        return value

    @model_validator(mode="after")
    def _require_bedrock_settings(self) -> Settings:
        problem = _missing_bedrock_settings(self.provider, self.model, self.aws_region)
        if problem:
            raise ValueError(problem)
        return self

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        provider = env.get("AI_PROVIDER", "mock").strip().lower()
        model = env.get("AI_MODEL", "").strip()
        region = (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip() or None
        trace_dir = env.get("TRACE_DIR", "").strip() or None
        try:
            return cls(provider=provider, model=model, aws_region=region, trace_dir=trace_dir)
        except ValidationError as exc:
            errors = _safe_messages(exc)
            # Field errors stop the model validator from running; report missing values too.
            problem = _missing_bedrock_settings(provider, model, region)
            if problem and problem not in errors:
                errors.append(problem)
            raise ConfigurationError(errors) from None


def _missing_bedrock_settings(provider: str, model: str, region: str | None) -> str | None:
    if provider != "bedrock":
        return None
    missing = [name for name, value in (("AI_MODEL", model), ("AWS_REGION", region)) if not value]
    return f"AI_PROVIDER=bedrock requires {', '.join(missing)}" if missing else None


def _safe_messages(exc: ValidationError) -> list[str]:
    messages = []
    for error in exc.errors(include_input=False, include_url=False):
        field = str(error["loc"][0]) if error["loc"] else ""
        message = error["msg"].removeprefix("Value error, ")
        if field in ENVIRONMENT_VARIABLES:
            messages.append(f"{ENVIRONMENT_VARIABLES[field]}: {message}")
        else:
            messages.append(message)
    return messages
