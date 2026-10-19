import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from cloud_incident_service.application import InvestigationService, UnverifiedEvidenceError
from cloud_incident_service.domain import (
    Evidence,
    Findings,
    IncidentReport,
    IncidentRequest,
    Provenance,
)
from cloud_incident_service.main import app, get_investigation_service
from cloud_incident_service.orchestration import DeterministicOrchestrator
from cloud_incident_service.tools import ToolRegistry, ToolSession, build_default_registry

FIXED_ID = UUID("00000000-0000-4000-8000-000000000042")
FIXED_TIME = datetime(2026, 10, 19, 11, 5, tzinfo=UTC)


def body(service="payments-api", environment="production", start="10:00", end="11:00", **extra):
    return {
        "service": service,
        "environment": environment,
        "symptoms": "Elevated errors",
        "time_range": {"start": f"2026-10-19T{start}:00Z", "end": f"2026-10-19T{end}:00Z"},
        **extra,
    }


class SpyOrchestrator(DeterministicOrchestrator):
    def __init__(self) -> None:
        self.calls: list[IncidentRequest] = []

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
        self.calls.append(request)
        return await super().investigate(request, tools)


@pytest.fixture
def spy() -> SpyOrchestrator:
    return SpyOrchestrator()


@pytest.fixture
def registry() -> ToolRegistry:
    return build_default_registry()


@pytest.fixture
def client(spy: SpyOrchestrator, registry: ToolRegistry):
    service = InvestigationService(spy, registry, clock=lambda: FIXED_TIME, new_id=lambda: FIXED_ID)
    app.dependency_overrides[get_investigation_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def investigate(client: TestClient, payload: dict) -> dict:
    response = client.post("/v1/investigations", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    IncidentReport.model_validate(data)
    return data


def statuses(report: dict) -> dict[str, str]:
    return {source["tool"]: source["status"] for source in report["data_sources"]}


def test_faulty_deployment_scenario_returns_evidence(client: TestClient) -> None:
    report = investigate(client, body())

    assert report["investigation_id"] == str(FIXED_ID)
    assert report["generated_at"] == "2026-10-19T11:05:00Z"
    assert report["status"] == "complete"
    assert statuses(report) == {
        "cloudwatch_signals": "ok",
        "deployment_history": "ok",
        "infrastructure_changes": "empty",
    }
    assert report["timeline"] == [
        "metric:payments-api:m-pay-002",
        "deploy:payments-api:d-0042",
        "alarm:payments-api:a-pay-001",
        "log:payments-api:l-pay-001",
        "metric:payments-api:m-pay-003",
        "metric:payments-api:m-pay-004",
        "log:payments-api:l-pay-002",
    ]
    assert [item["source_id"] for item in report["evidence"]] == report["timeline"]
    assert report["uncertainties"] == [
        {
            "description": "No data from infrastructure_changes for this service and time range.",
            "tool": "infrastructure_changes",
            "evidence_ids": [],
        }
    ]
    deploy = report["evidence"][1]
    assert deploy["summary"] == "Deployment d-0042 of version 1.8.0 from 1.7.3: succeeded"
    assert deploy["attributes"]["finished_at"] == "2026-10-19T10:08:00+00:00"


def test_other_services_and_environments_are_excluded(client: TestClient) -> None:
    report = investigate(client, body())

    assert {(item["service"], item["environment"]) for item in report["evidence"]} == {
        ("payments-api", "production")
    }
    ids = set(report["timeline"])
    assert "deploy:payments-api:d-0041" not in ids  # staging
    assert "alarm:orders-api:a-ord-001" not in ids  # other service


def test_signals_use_window_and_changes_use_lookback(client: TestClient) -> None:
    payments = set(investigate(client, body())["timeline"])
    assert "metric:payments-api:m-pay-001" not in payments  # 09:55, before the window
    assert "deploy:payments-api:d-0039" not in payments  # 08:30, before the look-back

    checkout = investigate(client, body("checkout-api", start="14:00", end="15:00"))
    ids = set(checkout["timeline"])
    assert "change:checkout-api:c-0502" in ids  # 13:40, inside the 60-minute look-back
    assert "change:checkout-api:c-0501" not in ids  # 12:30, outside the look-back
    assert "change:checkout-api:c-0504" not in ids  # staging
    assert statuses(checkout)["deployment_history"] == "empty"


def test_unavailable_source_makes_report_partial(client: TestClient) -> None:
    report = investigate(client, body("inventory-api", start="16:00", end="17:00"))

    assert report["status"] == "partial"
    assert statuses(report)["deployment_history"] == "unavailable"
    assert "alarm:inventory-api:a-inv-001" in report["timeline"]
    assert {
        "description": "deployment_history was unavailable, so its evidence is missing.",
        "tool": "deployment_history",
        "evidence_ids": [],
    } in report["uncertainties"]


def test_malformed_source_is_excluded_and_report_partial(client: TestClient) -> None:
    report = investigate(client, body("ledger-api", start="18:00", end="19:00"))

    assert report["status"] == "partial"
    assert statuses(report)["deployment_history"] == "malformed"
    assert report["timeline"] == ["metric:ledger-api:m-led-001"]
    assert not any(item["tool"] == "deployment_history" for item in report["evidence"])


def test_service_without_data_returns_empty_sources(client: TestClient) -> None:
    report = investigate(client, body("search-api"))

    assert report["status"] == "complete"
    assert set(statuses(report).values()) == {"empty"}
    assert report["evidence"] == []
    assert report["summary"] == (
        "No evidence was collected for search-api in production, so no findings could be made."
    )


def test_repeated_investigations_are_identical_apart_from_identity() -> None:
    service = InvestigationService(DeterministicOrchestrator(), build_default_registry())
    request = IncidentRequest.model_validate(body())

    first = asyncio.run(service.investigate(request)).model_dump(mode="json")
    second = asyncio.run(service.investigate(request)).model_dump(mode="json")

    assert first["investigation_id"] != second["investigation_id"]
    for report in (first, second):
        del report["investigation_id"], report["generated_at"]
    assert first == second


def test_default_service_is_wired_without_overrides() -> None:
    response = TestClient(app).post("/v1/investigations", json=body())

    assert response.status_code == 200
    assert len(response.json()["evidence"]) == 7


def test_no_registered_tools_returns_partial_report() -> None:
    service = InvestigationService(DeterministicOrchestrator(), ToolRegistry())

    report = asyncio.run(service.investigate(IncidentRequest.model_validate(body())))

    assert report.status == "partial"
    assert report.data_sources == []
    assert report.uncertainties[0].description == (
        "No evidence sources are registered, so none were queried."
    )


@pytest.mark.parametrize(
    "payload",
    [
        body(service="Payments API"),
        body(environment="qa"),
        {**body(), "symptoms": ""},
        {**body(), "symptoms": "x" * 2_001},
        {**body(), "time_range": {"start": "2026-10-19T10:00:00", "end": "2026-10-19T11:00:00Z"}},
        body(start="11:00", end="10:00"),
        {**body(), "time_range": {"start": "2026-10-18T09:00:00Z", "end": "2026-10-19T11:00:00Z"}},
        body(unexpected=True),
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
def test_invalid_input_returns_422_and_runs_nothing(client, spy, payload: dict) -> None:
    response = client.post("/v1/investigations", json=payload)

    assert response.status_code == 422
    assert spy.calls == []


def test_missing_body_returns_422(client, spy) -> None:
    response = client.post("/v1/investigations")

    assert response.status_code == 422
    assert spy.calls == []


class StaticEvidenceOrchestrator:
    """Returns the given evidence without querying tools, like a model inventing sources."""

    provenance = Provenance(orchestrator="model", model="example.model-v1:0")

    def __init__(self, evidence: Evidence, query_tools: bool = False) -> None:
        self.evidence = evidence
        self.query_tools = query_tools

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
        if self.query_tools:
            await DeterministicOrchestrator().investigate(request, tools)
        return Findings(status="partial", summary="Findings", evidence=[self.evidence])


def run_with(orchestrator) -> IncidentReport:
    service = InvestigationService(orchestrator, build_default_registry())
    return asyncio.run(service.investigate(IncidentRequest.model_validate(body())))


def payments_evidence(**overrides) -> Evidence:
    data = {
        "source_id": "deploy:payments-api:d-0042",
        "tool": "deployment_history",
        "service": "payments-api",
        "environment": "production",
        "timestamp": "2026-10-19T10:05:00Z",
        "kind": "deployment",
        "summary": "Deployment d-0042 of version 1.8.0 from 1.7.3: succeeded",
        "attributes": {
            "version": "1.8.0",
            "previous_version": "1.7.3",
            "status": "succeeded",
            "finished_at": "2026-10-19T10:08:00+00:00",
        },
    }
    data.update(overrides)
    return Evidence.model_validate(data)


def test_returned_evidence_passes_verification() -> None:
    report = run_with(StaticEvidenceOrchestrator(payments_evidence(), query_tools=True))

    assert [item.source_id for item in report.evidence] == ["deploy:payments-api:d-0042"]


def test_fabricated_evidence_is_rejected() -> None:
    with pytest.raises(UnverifiedEvidenceError, match="deploy:payments-api:d-0042"):
        run_with(StaticEvidenceOrchestrator(payments_evidence(), query_tools=False))


def test_altered_evidence_is_rejected() -> None:
    altered = payments_evidence(summary="Deployment d-0042 failed and corrupted the database")

    with pytest.raises(UnverifiedEvidenceError, match="deploy:payments-api:d-0042"):
        run_with(StaticEvidenceOrchestrator(altered, query_tools=True))


def test_evidence_outside_requested_scope_is_rejected() -> None:
    foreign = payments_evidence(source_id="deploy:orders-api:d-0107", service="orders-api")

    with pytest.raises(ValueError, match="evidence outside the requested service"):
        run_with(StaticEvidenceOrchestrator(foreign))
