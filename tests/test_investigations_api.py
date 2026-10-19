import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from cloud_incident_service.application import InvestigationService
from cloud_incident_service.domain import (
    Evidence,
    Findings,
    IncidentReport,
    IncidentRequest,
    Provenance,
)
from cloud_incident_service.main import app, get_investigation_service
from cloud_incident_service.orchestration import DeterministicOrchestrator

FIXED_ID = UUID("00000000-0000-4000-8000-000000000042")
FIXED_TIME = datetime(2026, 10, 19, 11, 5, tzinfo=UTC)
VALID_BODY = {
    "service": "payments-api",
    "environment": "production",
    "symptoms": "Elevated 5xx errors after 10:05 UTC",
    "time_range": {"start": "2026-10-19T18:00:00+08:00", "end": "2026-10-19T19:00:00+08:00"},
}


class SpyOrchestrator(DeterministicOrchestrator):
    def __init__(self) -> None:
        self.calls: list[IncidentRequest] = []

    async def investigate(self, request: IncidentRequest) -> Findings:
        self.calls.append(request)
        return await super().investigate(request)


@pytest.fixture
def spy() -> SpyOrchestrator:
    return SpyOrchestrator()


@pytest.fixture
def client(spy: SpyOrchestrator):
    service = InvestigationService(spy, clock=lambda: FIXED_TIME, new_id=lambda: FIXED_ID)
    app.dependency_overrides[get_investigation_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_valid_request_returns_schema_valid_partial_report(client, spy) -> None:
    response = client.post("/v1/investigations", json=VALID_BODY)

    assert response.status_code == 200
    body = response.json()
    IncidentReport.model_validate(body)
    assert body["investigation_id"] == str(FIXED_ID)
    assert body["generated_at"] == "2026-10-19T11:05:00Z"
    assert body["status"] == "partial"
    assert body["request"]["time_range"] == {
        "start": "2026-10-19T10:00:00Z",
        "end": "2026-10-19T11:00:00Z",
    }
    assert body["provenance"] == {
        "orchestrator": "deterministic",
        "model": "deterministic",
        "prompt_version": None,
    }
    assert body["uncertainties"] == [
        {
            "description": "No evidence sources are registered, so none were queried.",
            "tool": None,
            "evidence_ids": [],
        }
    ]
    for field in ("evidence", "timeline", "facts", "hypotheses", "next_steps"):
        assert body[field] == []
    assert body["remediation_proposals"] == []
    assert body["data_sources"] == []
    assert len(spy.calls) == 1


def test_default_service_is_wired_without_overrides() -> None:
    response = TestClient(app).post("/v1/investigations", json=VALID_BODY)

    assert response.status_code == 200
    UUID(response.json()["investigation_id"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"service": "Payments API"},
        {"environment": "qa"},
        {"symptoms": ""},
        {"symptoms": "x" * 2_001},
        {"time_range": {"start": "2026-10-19T10:00:00", "end": "2026-10-19T11:00:00Z"}},
        {"time_range": {"start": "2026-10-19T11:00:00Z", "end": "2026-10-19T10:00:00Z"}},
        {"time_range": {"start": "2026-10-18T09:00:00Z", "end": "2026-10-19T11:00:00Z"}},
        {"unexpected": True},
    ],
    ids=[
        "bad-service",
        "unknown-environment",
        "empty-symptoms",
        "oversized-symptoms",
        "naive-timestamp",
        "end-before-start",
        "window-over-24h",
        "unknown-field",
    ],
)
def test_invalid_input_returns_422_and_runs_nothing(client, spy, overrides: dict) -> None:
    response = client.post("/v1/investigations", json={**VALID_BODY, **overrides})

    assert response.status_code == 422
    assert spy.calls == []


def test_missing_body_returns_422(client, spy) -> None:
    response = client.post("/v1/investigations")

    assert response.status_code == 422
    assert spy.calls == []


class ForeignEvidenceOrchestrator:
    provenance = Provenance(orchestrator="deterministic", model="deterministic")

    async def investigate(self, request: IncidentRequest) -> Findings:
        evidence = Evidence(
            source_id="deploy:orders-api:d-1",
            tool="deployment_history",
            service="orders-api",
            environment=request.environment,
            timestamp=request.time_range.start,
            kind="deployment",
            summary="Unrelated deployment",
        )
        return Findings(status="partial", summary="Bad findings", evidence=[evidence])


def test_service_rejects_findings_outside_requested_scope() -> None:
    service = InvestigationService(ForeignEvidenceOrchestrator())
    request = IncidentRequest.model_validate(VALID_BODY)

    with pytest.raises(ValueError, match="evidence outside the requested service"):
        asyncio.run(service.investigate(request))
