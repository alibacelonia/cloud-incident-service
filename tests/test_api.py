import socket

import pytest
from fastapi.testclient import TestClient

from cloud_incident_service.application import InvestigationService
from cloud_incident_service.config import ConfigurationError
from cloud_incident_service.domain import Findings, IncidentRequest, Provenance
from cloud_incident_service.main import app, get_investigation_service
from cloud_incident_service.providers import ProviderInvocationError
from cloud_incident_service.tools import ToolRegistry, ToolSession

INVESTIGATION = {
    "service": "payments-api",
    "environment": "production",
    "symptoms": "Elevated 5xx errors",
    "time_range": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"},
}


class RaisingOrchestrator:
    """Stands in for a model-backed orchestrator whose provider or settings fail."""

    provenance = Provenance(orchestrator="model", model="example.model-v1:0")

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
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


def use_orchestrator(orchestrator) -> None:
    service = InvestigationService(orchestrator, ToolRegistry())
    app.dependency_overrides[get_investigation_service] = lambda: service


def test_configuration_error_at_request_time_returns_503(client: TestClient) -> None:
    use_orchestrator(RaisingOrchestrator(ConfigurationError(["AI_MODEL: example problem"])))

    response = client.post("/v1/investigations", json=INVESTIGATION)

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Invalid configuration: AI_MODEL: example problem",
        "code": "configuration_invalid",
    }


@pytest.mark.parametrize(("unavailable", "status_code"), [(True, 503), (False, 502)])
def test_provider_failures_return_safe_error(
    client: TestClient, unavailable: bool, status_code: int
) -> None:
    error = ProviderInvocationError("provider_example", "Safe message.", unavailable=unavailable)
    use_orchestrator(RaisingOrchestrator(error))

    response = client.post("/v1/investigations", json=INVESTIGATION)

    assert response.status_code == status_code
    assert response.json() == {"detail": "Safe message.", "code": "provider_example"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/generate"),
        ("post", "/v1/rag"),
        ("post", "/v1/documents"),
        ("post", "/v1/documents/preview"),
    ],
)
def test_starter_endpoints_are_removed(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, json={"input": "hello"})

    assert response.status_code == 404


def test_openapi_lists_only_supported_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert sorted(paths) == ["/health", "/v1/investigations"]


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


def test_test_client_uses_the_supported_http_backend() -> None:
    # M-01: Starlette deprecated its httpx fallback; httpx2 avoids the deprecation warning.
    import starlette.testclient

    assert starlette.testclient.httpx.__name__ == "httpx2"
