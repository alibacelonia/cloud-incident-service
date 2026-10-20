"""Opt-in configuration for the live log source and its fail-closed rules (V2-01, V2-08)."""

import json

import boto3
import pytest
from fastapi.testclient import TestClient

from cloud_incident_service import main
from cloud_incident_service.config import ConfigurationError, Settings
from cloud_incident_service.main import app
from cloud_incident_service.tools import build_default_registry

GROUPS = json.dumps({"payments-api/production": "/example/payments-api/prod"})
LIVE = {"LOG_SOURCE": "cloudwatch", "CLOUDWATCH_LOG_GROUPS": GROUPS, "AWS_REGION": "us-east-1"}
BEDROCK = {"AI_PROVIDER": "bedrock", "AI_MODEL": "example.model-v1:0", "AWS_REGION": "us-east-1"}
BODY = {
    "service": "payments-api",
    "environment": "production",
    "symptoms": "Elevated 5xx errors",
    "time_range": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"},
}


class FakeLogs:
    def __init__(self) -> None:
        self.calls = 0

    def filter_log_events(self, **kwargs):
        self.calls += 1
        return {"events": []}


@pytest.fixture(autouse=True)
def fresh_registry_cache(monkeypatch):
    monkeypatch.setattr(main, "_registries", {})


@pytest.fixture
def logs_clients(monkeypatch) -> list[FakeLogs]:
    """Replaces the AWS client factory and records every client the app builds."""
    created: list[FakeLogs] = []

    def fake_factory(region):
        created.append(FakeLogs())
        return created[-1]

    monkeypatch.setattr("cloud_incident_service.tools.create_logs_client", fake_factory)
    return created


def apply(monkeypatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)


# Settings


def test_live_logs_are_off_by_default() -> None:
    settings = Settings.from_environment({})

    assert settings.log_source == "fixture"
    assert settings.cloudwatch_log_groups == {}
    assert settings.allow_live_evidence_to_model is False


def test_allowlist_is_parsed_from_json() -> None:
    settings = Settings.from_environment(LIVE)

    assert settings.log_source == "cloudwatch"
    assert settings.cloudwatch_log_groups == {
        "payments-api/production": "/example/payments-api/prod"
    }


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[1, 2]",
        json.dumps({"payments-api": "/example/group"}),
        json.dumps({"Payments-API/production": "/example/group"}),
        json.dumps({"payments-api/qa": "/example/group"}),
        json.dumps({"payments-api/production": "bad group name with spaces"}),
        json.dumps({"payments-api/production": ""}),
        json.dumps({"payments-api/production": 7}),
        json.dumps({f"svc-{i}/production": "/g" for i in range(51)}),
    ],
)
def test_invalid_allowlists_name_the_variable_without_echoing_values(value: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({**LIVE, "CLOUDWATCH_LOG_GROUPS": value})

    [message] = raised.value.errors
    assert message.startswith("CLOUDWATCH_LOG_GROUPS: ")
    assert "bad group name" not in str(raised.value) and "not json" not in str(raised.value)


def test_cloudwatch_requires_an_allowlist_and_a_region() -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"LOG_SOURCE": "cloudwatch"})

    assert raised.value.errors == [
        "LOG_SOURCE=cloudwatch requires CLOUDWATCH_LOG_GROUPS, AWS_REGION"
    ]


def test_unknown_log_source_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"LOG_SOURCE": "elasticsearch"})

    assert raised.value.errors == ["LOG_SOURCE: Input should be 'fixture' or 'cloudwatch'"]


@pytest.mark.parametrize("value", ["maybe", "2", "enable"])
def test_invalid_opt_in_values_are_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({**LIVE, "ALLOW_LIVE_EVIDENCE_TO_MODEL": value})

    assert raised.value.errors[0].startswith("ALLOW_LIVE_EVIDENCE_TO_MODEL: ")


# V2-08: live evidence reaches a real model only with explicit approval


def test_live_logs_with_a_real_model_fail_closed() -> None:
    env = {**LIVE, **BEDROCK, "ORCHESTRATOR": "model"}

    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment(env)

    assert "set ALLOW_LIVE_EVIDENCE_TO_MODEL=1" in raised.value.errors[0]
    allowed = Settings.from_environment({**env, "ALLOW_LIVE_EVIDENCE_TO_MODEL": "1"})
    assert allowed.allow_live_evidence_to_model is True


@pytest.mark.parametrize(
    "env",
    [
        {**LIVE, **BEDROCK, "ORCHESTRATOR": "deterministic"},
        {**LIVE, "AI_PROVIDER": "mock", "ORCHESTRATOR": "model"},
        {**BEDROCK, "ORCHESTRATOR": "model"},
    ],
    ids=["deterministic", "offline-stand-in", "fixture-logs"],
)
def test_combinations_that_send_no_live_evidence_to_a_model_are_allowed(env) -> None:
    Settings.from_environment(env)


def test_anthropic_provider_is_also_blocked_without_approval() -> None:
    env = {
        **LIVE,
        "AI_PROVIDER": "anthropic",
        "AI_MODEL": "example-model",
        "ANTHROPIC_API_KEY": "sk-ant-" + "k" * 40,
        "ORCHESTRATOR": "model",
    }

    with pytest.raises(ConfigurationError, match="ALLOW_LIVE_EVIDENCE_TO_MODEL"):
        Settings.from_environment(env)


# V2-01: no opt-in, no client


def test_default_registry_never_touches_aws(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("an AWS session was created without opting in")

    monkeypatch.setattr(boto3.session, "Session", forbidden)

    registry = build_default_registry(Settings())
    assert build_default_registry().tool_names == registry.tool_names


def test_app_uses_fixtures_and_builds_no_client_by_default(logs_clients) -> None:
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/v1/investigations", json=BODY)

    assert response.status_code == 200
    assert {s["origin"] for s in response.json()["data_sources"]} == {"fixture"}
    assert logs_clients == []


def test_opting_in_builds_one_client_and_serves_live_logs(monkeypatch, logs_clients) -> None:
    apply(monkeypatch, LIVE)

    with TestClient(app) as client:
        first = client.post("/v1/investigations", json=BODY)
        second = client.post("/v1/investigations", json=BODY)

    assert first.status_code == second.status_code == 200
    sources = {s["tool"]: s for s in first.json()["data_sources"]}
    assert sources["cloudwatch_signals"] == {
        "tool": "cloudwatch_signals",
        "status": "empty",
        "origin": "live",
    }
    assert sources["deployment_history"]["origin"] == "fixture"
    assert len(logs_clients) == 1  # created at startup, reused by both requests
    assert logs_clients[0].calls == 2
    assert any(
        item["description"].startswith("Mixed sources") for item in first.json()["uncertainties"]
    )


def test_changed_configuration_gets_its_own_registry(monkeypatch, logs_clients) -> None:
    apply(monkeypatch, LIVE)
    client = TestClient(app)
    client.post("/v1/investigations", json=BODY)
    monkeypatch.setenv("CLOUDWATCH_LOG_GROUPS", json.dumps({"payments-api/production": "/other"}))

    client.post("/v1/investigations", json=BODY)

    assert len(logs_clients) == 2


# Startup and readiness


def test_startup_fails_closed_for_live_evidence_to_a_model(monkeypatch, logs_clients) -> None:
    apply(monkeypatch, {**LIVE, **BEDROCK, "ORCHESTRATOR": "model"})

    with pytest.raises(ConfigurationError, match="ALLOW_LIVE_EVIDENCE_TO_MODEL"), TestClient(app):
        pass
    assert logs_clients == []


def test_health_reports_a_live_log_misconfiguration(monkeypatch, logs_clients) -> None:
    apply(monkeypatch, {"LOG_SOURCE": "cloudwatch"})

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["errors"] == [
        "LOG_SOURCE=cloudwatch requires CLOUDWATCH_LOG_GROUPS, AWS_REGION"
    ]


def test_startup_logs_that_live_logs_are_in_use(monkeypatch, logs_clients, caplog) -> None:
    apply(monkeypatch, LIVE)

    with caplog.at_level("WARNING"), TestClient(app):
        pass

    assert "LOG_SOURCE=cloudwatch: cloudwatch_signals reads live logs from 1 allowlisted" in (
        caplog.text
    )
    assert "/example/payments-api/prod" not in caplog.text
