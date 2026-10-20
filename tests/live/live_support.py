"""Opt-in rules for live model tests against Amazon Bedrock or the Anthropic API (NFR-3, D-6)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cloud_incident_service.config import ConfigurationError, Settings

# D-6: the live evaluation must pass at least 80% of scenarios.
LIVE_PASS_THRESHOLD = 0.8
# Per-test wall-clock bounds, on top of the orchestrator's own 60-second loop limit.
SMOKE_TIMEOUT_SECONDS = 60
EVALUATION_TIMEOUT_SECONDS = 15 * 60
# Small output caps keep live runs cheap.
SMOKE_MAX_OUTPUT_TOKENS = 64
INVESTIGATION_MAX_OUTPUT_TOKENS = 2_048


def live_provider(environ: Mapping[str, str]) -> str:
    """`anthropic` when AI_PROVIDER says so; otherwise Bedrock, as before the Anthropic client."""
    return (
        "anthropic" if environ.get("AI_PROVIDER", "").strip().lower() == "anthropic" else "bedrock"
    )


def live_skip_reason(environ: Mapping[str, str]) -> str | None:
    """Why live tests cannot run with this environment, or None when they can."""
    if environ.get("RUN_LIVE_TESTS") != "1":
        return (
            "live tests are disabled; set RUN_LIVE_TESTS=1 to run them against a live model "
            "(Amazon Bedrock, or the Anthropic API with AI_PROVIDER=anthropic)"
        )
    provider = live_provider(environ)
    if not environ.get("AI_MODEL", "").strip():
        return f"AI_MODEL is not set; use a model ID you may invoke on {provider}"
    if provider == "bedrock":
        if not (environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION") or "").strip():
            return "AWS_REGION is not set"
    elif not environ.get("ANTHROPIC_API_KEY", "").strip():
        return "ANTHROPIC_API_KEY is not set"
    try:
        live_settings(environ)
    except ConfigurationError as exc:
        return f"invalid live configuration: {exc}"
    return None


def credentials_skip_reason(session: Any) -> str | None:
    """Why AWS credentials cannot be used, or None when they load.

    Credentials are resolved eagerly: SSO and assume-role credentials load lazily, so an expired
    session would otherwise pass this check and fail on every Bedrock call instead.
    """
    from botocore.exceptions import BotoCoreError

    try:
        credentials = session.get_credentials()
        if credentials is None:
            return "AWS credentials were not found in the default credential chain"
        credentials.get_frozen_credentials()
    except BotoCoreError as exc:
        return (
            f"AWS credentials could not be loaded ({type(exc).__name__}); "
            "for an SSO profile, run `aws sso login --profile <profile>` and retry"
        )
    return None


def live_settings(environ: Mapping[str, str]) -> Settings:
    """Settings for live runs: the live provider and the model orchestrator, no trace files."""
    return Settings.from_environment(
        {**environ, "AI_PROVIDER": live_provider(environ), "ORCHESTRATOR": "model", "TRACE_DIR": ""}
    )
