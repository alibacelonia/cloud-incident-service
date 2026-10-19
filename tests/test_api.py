import socket

import pytest
from fastapi.testclient import TestClient

from cloud_incident_service.config import ConfigurationError
from cloud_incident_service.main import app, get_provider
from cloud_incident_service.providers import ProviderInvocationError


class FakeProvider:
    async def generate(self, prompt: str) -> str:
        assert "User input:" in prompt
        return "controlled-test-output"


class FailingProvider:
    def __init__(self, error: ProviderInvocationError) -> None:
        self.error = error

    async def generate(self, prompt: str) -> str:
        raise self.error


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_requires_no_api_key(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider": "mock", "errors": []}


def test_health_reports_misconfiguration(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "bedrock")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "misconfigured",
        "provider": None,
        "errors": ["AI_PROVIDER=bedrock requires AI_MODEL, AWS_REGION"],
    }


def test_health_is_ok_for_complete_bedrock_settings(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "bedrock")
    monkeypatch.setenv("AI_MODEL", "example.model-v1:0")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["provider"] == "bedrock"


def test_generate_uses_injected_provider(client: TestClient) -> None:
    app.dependency_overrides[get_provider] = lambda: FakeProvider()

    response = client.post("/v1/generate", json={"input": "hello"})

    assert response.status_code == 200
    assert response.json() == {"output": "controlled-test-output"}


def test_generate_validates_empty_input(client: TestClient) -> None:
    response = client.post("/v1/generate", json={"input": ""})

    assert response.status_code == 422


def test_unknown_provider_is_reported_without_network(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "unknown")

    response = client.post("/v1/generate", json={"input": "hello"})

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Invalid configuration: AI_PROVIDER: Input should be 'mock' or 'bedrock'",
        "code": "configuration_invalid",
    }


@pytest.mark.parametrize(("unavailable", "status_code"), [(True, 503), (False, 502)])
def test_provider_failures_return_safe_error(
    client: TestClient, unavailable: bool, status_code: int
) -> None:
    error = ProviderInvocationError("provider_example", "Safe message.", unavailable=unavailable)
    app.dependency_overrides[get_provider] = lambda: FailingProvider(error)

    response = client.post("/v1/generate", json={"input": "hello"})

    assert response.status_code == status_code
    assert response.json() == {"detail": "Safe message.", "code": "provider_example"}


def test_startup_fails_on_invalid_configuration(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "unknown")

    with pytest.raises(ConfigurationError, match="AI_PROVIDER"), TestClient(app):
        pass


def test_startup_succeeds_with_default_configuration() -> None:
    with TestClient(app) as started:
        assert started.get("/health").status_code == 200


def test_default_suite_blocks_network_access() -> None:
    with pytest.raises(RuntimeError, match="Network access is disabled"):
        socket.create_connection(("example.com", 443))
