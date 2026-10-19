"""Offline checks that live tests stay opt-in (AC-12). These run in the default suite."""

import tomllib
from pathlib import Path

import pytest
from live_support import LIVE_PASS_THRESHOLD, live_settings, live_skip_reason

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


def test_complete_configuration_enables_live_tests() -> None:
    assert live_skip_reason(CONFIGURED) is None
    assert (
        live_skip_reason({**CONFIGURED, "AWS_REGION": "", "AWS_DEFAULT_REGION": "eu-west-1"})
        is None
    )


def test_live_settings_force_bedrock_model_orchestrator_without_trace_files() -> None:
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
