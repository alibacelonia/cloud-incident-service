from fastapi.testclient import TestClient

from cloud_incident_service.main import app, get_provider


class FakeProvider:
    async def generate(self, prompt: str) -> str:
        assert "User input:" in prompt
        return "controlled-test-output"


def test_health_requires_no_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_generate_uses_injected_provider() -> None:
    app.dependency_overrides[get_provider] = lambda: FakeProvider()
    try:
        response = TestClient(app).post("/v1/generate", json={"input": "hello"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"output": "controlled-test-output"}


def test_generate_validates_empty_input() -> None:
    response = TestClient(app).post("/v1/generate", json={"input": ""})

    assert response.status_code == 422


def test_unknown_provider_is_reported_without_network(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER", "unknown")

    response = TestClient(app).post("/v1/generate", json={"input": "hello"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Unsupported AI provider: unknown"}
