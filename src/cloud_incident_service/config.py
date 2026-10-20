from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

# Maps settings fields to the environment variables they are read from.
ENVIRONMENT_VARIABLES: dict[str, str] = {
    "provider": "AI_PROVIDER",
    "model": "AI_MODEL",
    "aws_region": "AWS_REGION",
    "trace_dir": "TRACE_DIR",
    "orchestrator": "ORCHESTRATOR",
    "model_max_output_tokens": "MODEL_MAX_OUTPUT_TOKENS",
    "service_api_key": "SERVICE_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
}


MIN_API_KEY_LENGTH = 32
MAX_API_KEY_LENGTH = 256
MIN_PROVIDER_KEY_LENGTH = 20
MAX_PROVIDER_KEY_LENGTH = 512


class ConfigurationError(RuntimeError):
    """Raised when settings are invalid. Messages name variables, never their values."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Invalid configuration: " + "; ".join(errors))


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["mock", "bedrock", "anthropic"] = "mock"
    model: str = Field(default="", max_length=256, pattern=r"^\S*$")
    aws_region: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[a-z]+)+-\d+$")
    # D-3: traces are written only when a directory is configured.
    trace_dir: Path | None = None
    orchestrator: Literal["deterministic", "model"] = "deterministic"
    # A-7: output-token cap for each model turn.
    model_max_output_tokens: int = Field(default=2_048, ge=256, le=8_192)
    # SEC-7: when set, /v1/ requests need a matching X-API-Key header (ADR-017).
    service_api_key: SecretStr | None = None
    # Credential for AI_PROVIDER=anthropic (ADR-018); never logged, traced, or echoed.
    anthropic_api_key: SecretStr | None = None

    @field_validator("anthropic_api_key")
    @classmethod
    def _check_anthropic_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        secret = value.get_secret_value()
        valid = (
            MIN_PROVIDER_KEY_LENGTH <= len(secret) <= MAX_PROVIDER_KEY_LENGTH
            and secret.isascii()
            and not any(char.isspace() for char in secret)
        )
        if not valid:
            raise ValueError(
                f"must be {MIN_PROVIDER_KEY_LENGTH}-{MAX_PROVIDER_KEY_LENGTH} ASCII characters "
                "without whitespace"
            )
        return value

    @field_validator("service_api_key")
    @classmethod
    def _check_service_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        secret = value.get_secret_value()
        if not MIN_API_KEY_LENGTH <= len(secret) <= MAX_API_KEY_LENGTH or not secret.isascii():
            raise ValueError(
                f"must be {MIN_API_KEY_LENGTH}-{MAX_API_KEY_LENGTH} ASCII characters; "
                'generate one with python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if any(char.isspace() for char in secret):
            raise ValueError("must not contain whitespace")
        return value

    @field_validator("trace_dir", mode="before")
    @classmethod
    def _check_trace_dir(cls, value: object) -> object:
        if isinstance(value, str) and ("\x00" in value or len(value) > 1_024):
            raise ValueError("must be a directory path of at most 1024 characters")
        return value

    @model_validator(mode="after")
    def _require_provider_settings(self) -> Settings:
        problem = _missing_provider_settings(
            self.provider, self.model, self.aws_region, self.anthropic_api_key is not None
        )
        if problem:
            raise ValueError(problem)
        return self

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        provider = env.get("AI_PROVIDER", "mock").strip().lower()
        model = env.get("AI_MODEL", "").strip()
        region = (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip() or None
        optional = {
            field: value
            for field, value in (
                ("trace_dir", env.get("TRACE_DIR", "").strip()),
                ("orchestrator", env.get("ORCHESTRATOR", "").strip().lower()),
                ("model_max_output_tokens", env.get("MODEL_MAX_OUTPUT_TOKENS", "").strip()),
                ("service_api_key", env.get("SERVICE_API_KEY", "").strip()),
                ("anthropic_api_key", env.get("ANTHROPIC_API_KEY", "").strip()),
            )
            if value
        }
        try:
            return cls(provider=provider, model=model, aws_region=region, **optional)
        except ValidationError as exc:
            errors = _safe_messages(exc)
            # Field errors stop the model validator from running; report missing values too.
            problem = _missing_provider_settings(
                provider, model, region, bool(optional.get("anthropic_api_key"))
            )
            if problem and problem not in errors:
                errors.append(problem)
            raise ConfigurationError(errors) from None


def _missing_provider_settings(
    provider: str, model: str, region: str | None, has_anthropic_key: bool
) -> str | None:
    required: dict[str, list[tuple[str, bool]]] = {
        "bedrock": [("AI_MODEL", bool(model)), ("AWS_REGION", bool(region))],
        "anthropic": [("AI_MODEL", bool(model)), ("ANTHROPIC_API_KEY", has_anthropic_key)],
    }
    missing = [name for name, present in required.get(provider, []) if not present]
    return f"AI_PROVIDER={provider} requires {', '.join(missing)}" if missing else None


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
