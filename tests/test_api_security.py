"""API-key authentication and request-size limits (SEC-6, SEC-7, ADR-017)."""

import hashlib
import json
import logging

import pytest
from fastapi.testclient import TestClient

from cloud_incident_service import api_security
from cloud_incident_service.api_security import MAX_BODY_BYTES, api_key_matches
from cloud_incident_service.config import ConfigurationError, Settings
from cloud_incident_service.main import app

KEY = "k" * 43
OTHER = "z" * 43
BODY = {
    "service": "payments-api",
    "environment": "production",
    "symptoms": "Elevated 5xx errors",
    "time_range": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"},
}
UNAUTHORIZED = {"detail": "A valid API key is required.", "code": "unauthorized"}
TOO_LARGE = {"detail": "The request body is too large.", "code": "request_too_large"}


@pytest.fixture
def secured(monkeypatch) -> TestClient:
    monkeypatch.setenv("SERVICE_API_KEY", KEY)
    return TestClient(app)


def post(client: TestClient, key: str | None = KEY, **kwargs):
    headers = {"X-API-Key": key} if key is not None else {}
    return client.post("/v1/investigations", json=BODY, headers=headers, **kwargs)


# Settings


def test_api_key_is_optional_and_off_by_default() -> None:
    assert Settings.from_environment({}).service_api_key is None
    assert Settings.from_environment({"SERVICE_API_KEY": "  "}).service_api_key is None


def test_api_key_is_trimmed_and_kept_secret() -> None:
    settings = Settings.from_environment({"SERVICE_API_KEY": f"  {KEY}\n"})

    assert settings.service_api_key.get_secret_value() == KEY
    assert KEY not in repr(settings)
    assert KEY not in str(settings.model_dump())


@pytest.mark.parametrize("value", ["short", "x" * 31, "x" * 257, "a b" * 20, "é" * 40])
def test_invalid_api_keys_name_the_variable_without_echoing_the_value(value: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"SERVICE_API_KEY": value})

    [message] = raised.value.errors
    assert message.startswith("SERVICE_API_KEY: ")
    assert value not in str(raised.value)
    assert "secrets.token_urlsafe" in message or "whitespace" in message


# Authentication


def test_api_is_open_when_no_key_is_configured() -> None:
    assert post(TestClient(app), key=None).status_code == 200


def test_correct_key_is_accepted(secured) -> None:
    assert post(secured).status_code == 200


@pytest.mark.parametrize("key", [None, "", OTHER, KEY[:-1], KEY + "x", KEY.upper()])
def test_missing_or_wrong_key_returns_401(secured, key) -> None:
    response = post(secured, key=key)

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert response.headers["www-authenticate"] == 'ApiKey realm="cloud-incident-service"'


def test_authentication_runs_before_body_validation(secured) -> None:
    assert secured.post("/v1/investigations", json={"service": "!"}).status_code == 401
    malformed = secured.post(
        "/v1/investigations", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert malformed.status_code == 401
    # With the key, the same bad input reaches validation.
    valid_key = {"X-API-Key": KEY}
    assert (
        secured.post("/v1/investigations", json={"service": "!"}, headers=valid_key).status_code
        == 422
    )


def test_duplicate_key_headers_are_rejected(secured) -> None:
    response = secured.post(
        "/v1/investigations", json=BODY, headers=[("X-API-Key", KEY), ("X-API-Key", KEY)]
    )

    assert response.status_code == 401


def test_header_name_is_case_insensitive(secured) -> None:
    assert (
        secured.post("/v1/investigations", json=BODY, headers={"x-api-key": KEY}).status_code == 200
    )


@pytest.mark.parametrize("path", ["/health", "/docs", "/openapi.json"])
def test_health_and_docs_stay_open(secured, path: str) -> None:
    assert secured.get(path).status_code == 200


def test_unknown_v1_routes_require_a_key_too(secured) -> None:
    assert secured.get("/v1/anything").status_code == 401
    assert secured.get("/v1/anything", headers={"X-API-Key": KEY}).status_code == 404


def test_key_comparison_is_constant_time(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    real = api_security.hmac.compare_digest

    def spy(left, right):
        calls.append((len(left), len(right)))
        return real(left, right)

    monkeypatch.setattr(api_security.hmac, "compare_digest", spy)

    assert api_key_matches(KEY, KEY) and not api_key_matches("short", KEY)
    # Digests have a fixed length whatever the key length is.
    assert calls == [(32, 32), (32, 32)]
    assert hashlib.sha256().digest_size == 32


def test_rejected_keys_are_never_logged(secured, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        post(secured, key=OTHER)
        post(secured, key=None)
        post(secured)

    assert OTHER not in caplog.text and KEY not in caplog.text
    assert "Rejected request: missing or invalid API key" in caplog.text


def test_invalid_configuration_returns_503_for_protected_routes(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_API_KEY", "too-short")

    response = post(TestClient(app), key=None)

    assert response.status_code == 503
    assert response.json()["code"] == "configuration_invalid"
    assert "too-short" not in response.text


def test_openapi_documents_the_api_key_scheme() -> None:
    schema = TestClient(app).get("/openapi.json").json()

    assert schema["components"]["securitySchemes"]["ApiKeyAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    assert {"ApiKeyAuth": []} in schema["paths"]["/v1/investigations"]["post"]["security"]
    assert {"401", "413"} <= set(schema["paths"]["/v1/investigations"]["post"]["responses"])


# Request size


def test_oversized_declared_body_returns_413_even_with_a_valid_key(secured) -> None:
    padded = {**BODY, "symptoms": "x" * (MAX_BODY_BYTES + 1)}

    response = secured.post("/v1/investigations", json=padded, headers={"X-API-Key": KEY})

    assert response.status_code == 413
    assert response.json() == TOO_LARGE


def test_oversized_streamed_body_returns_413(secured) -> None:
    def chunks():
        yield b'{"symptoms": "'
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024
        yield b'"}'

    response = secured.post(
        "/v1/investigations",
        content=chunks(),
        headers={"X-API-Key": KEY, "content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == TOO_LARGE


def test_unauthenticated_oversized_requests_get_401_not_413(secured) -> None:
    padded = {**BODY, "symptoms": "x" * (MAX_BODY_BYTES + 1)}

    assert secured.post("/v1/investigations", json=padded).status_code == 401


def test_body_limit_applies_when_authentication_is_off() -> None:
    padded = {**BODY, "symptoms": "x" * (MAX_BODY_BYTES + 1)}

    assert TestClient(app).post("/v1/investigations", json=padded).status_code == 413


def test_maximum_valid_request_fits_under_the_limit() -> None:
    # 2,000 four-byte characters is the largest symptoms value FR-1 allows.
    largest = {**BODY, "symptoms": "𝔘" * 2_000}

    assert len(json.dumps(largest, ensure_ascii=False).encode()) < MAX_BODY_BYTES
    assert TestClient(app).post("/v1/investigations", json=largest).status_code == 200
