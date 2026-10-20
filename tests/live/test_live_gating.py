"""Offline checks that live tests stay opt-in (AC-12). These run in the default suite."""

import tomllib
from pathlib import Path

import pytest
from botocore.exceptions import ProfileNotFound, TokenRetrievalError
from live_support import (
    LIVE_PASS_THRESHOLD,
    credentials_skip_reason,
    live_provider,
    live_settings,
    live_skip_reason,
)

CONFIGURED = {"RUN_LIVE_TESTS": "1", "AI_MODEL": "example.model-v1:0", "AWS_REGION": "us-east-1"}


@pytest.mark.parametrize(
    ("environ", "reason"),
    [
        ({}, "live tests are disabled; set RUN_LIVE_TESTS=1"),
        ({**CONFIGURED, "RUN_LIVE_TESTS": "true"}, "live tests are disabled"),
        ({**CONFIGURED, "AI_MODEL": " "}, "AI_MODEL is not set"),
        ({"RUN_LIVE_TESTS": "1", "AI_MODEL": "example.model-v1:0"}, "AWS_REGION is not set"),
        (
            {**CONFIGURED, "AWS_REGION": "Mars-1"},
            "invalid live configuration: Invalid configuration: AWS_REGION",
        ),
    ],
)
def test_live_tests_skip_with_a_clear_reason(environ: dict, reason: str) -> None:
    assert live_skip_reason(environ).startswith(reason)


ANTHROPIC = {
    "RUN_LIVE_TESTS": "1",
    "AI_PROVIDER": "anthropic",
    "AI_MODEL": "example-model",
    "ANTHROPIC_API_KEY": "sk-ant-" + "k" * 40,
}


def test_anthropic_live_tests_need_a_key_but_not_a_region() -> None:
    assert live_skip_reason(ANTHROPIC) is None
    assert live_skip_reason({**ANTHROPIC, "ANTHROPIC_API_KEY": ""}) == (
        "ANTHROPIC_API_KEY is not set"
    )
    assert live_skip_reason({**ANTHROPIC, "AI_MODEL": ""}).startswith("AI_MODEL is not set")


def test_live_provider_defaults_to_bedrock() -> None:
    assert live_provider({}) == "bedrock"
    assert live_provider({"AI_PROVIDER": "mock"}) == "bedrock"
    assert live_provider({"AI_PROVIDER": " Anthropic "}) == "anthropic"


def test_complete_configuration_enables_live_tests() -> None:
    assert live_skip_reason(CONFIGURED) is None
    assert (
        live_skip_reason({**CONFIGURED, "AWS_REGION": "", "AWS_DEFAULT_REGION": "eu-west-1"})
        is None
    )


def test_live_settings_force_the_model_orchestrator_without_trace_files() -> None:
    settings = live_settings({**CONFIGURED, "AI_PROVIDER": "mock", "TRACE_DIR": "/tmp/x"})

    assert (settings.provider, settings.orchestrator, settings.trace_dir) == (
        "bedrock",
        "model",
        None,
    )


def test_live_threshold_matches_decision_d6() -> None:
    assert LIVE_PASS_THRESHOLD == 0.8


def test_live_tests_are_deselected_by_default() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    options = tomllib.loads(pyproject.read_text())["tool"]["pytest"]["ini_options"]

    assert options["addopts"] == "-m 'not live'"
    assert any(marker.startswith("live:") for marker in options["markers"])


class FakeCredentials:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def get_frozen_credentials(self):
        if self.error is not None:
            raise self.error
        return object()


class FakeSession:
    def __init__(self, credentials=None, error: Exception | None = None) -> None:
        self.credentials = credentials
        self.error = error

    def get_credentials(self):
        if self.error is not None:
            raise self.error
        return self.credentials


def test_credentials_that_load_enable_live_tests() -> None:
    assert credentials_skip_reason(FakeSession(FakeCredentials())) is None


def test_missing_credentials_skip_live_tests() -> None:
    assert credentials_skip_reason(FakeSession(None)) == (
        "AWS credentials were not found in the default credential chain"
    )


@pytest.mark.parametrize(
    "session",
    [
        # An expired SSO session: credentials exist but fail when resolved.
        FakeSession(
            FakeCredentials(TokenRetrievalError(provider="sso", error_msg="Token has expired"))
        ),
        FakeSession(error=ProfileNotFound(profile="missing")),
    ],
    ids=["expired-sso", "unknown-profile"],
)
def test_unloadable_credentials_skip_with_login_hint(session) -> None:
    reason = credentials_skip_reason(session)

    assert reason.startswith("AWS credentials could not be loaded (")
    assert "aws sso login" in reason


def test_anthropic_live_settings_use_the_anthropic_provider() -> None:
    settings = live_settings({**ANTHROPIC, "TRACE_DIR": "/tmp/x"})

    assert (settings.provider, settings.orchestrator, settings.trace_dir) == (
        "anthropic",
        "model",
        None,
    )
