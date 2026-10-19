import asyncio
from datetime import UTC, datetime

import pytest

from cloud_incident_service.application import InvestigationService
from cloud_incident_service.domain import Evidence, IncidentReport, IncidentRequest, ToolResult
from cloud_incident_service.orchestration import DeterministicOrchestrator
from cloud_incident_service.orchestration.findings import build_findings
from cloud_incident_service.tools import build_default_registry

SCENARIOS = {
    "payments-api": ("10:00", "11:00"),
    "checkout-api": ("14:00", "15:00"),
    "orders-api": ("10:00", "11:00"),
    "cart-api": ("12:00", "13:00"),
    "billing-api": ("10:00", "11:00"),
    "auth-api": ("15:00", "16:00"),
    "inventory-api": ("16:00", "17:00"),
    "ledger-api": ("18:00", "19:00"),
    "search-api": ("10:00", "11:00"),
    "notify-api": ("10:00", "11:00"),
}


def request_for(service: str, symptoms: str = "Elevated 5xx errors") -> IncidentRequest:
    start, end = SCENARIOS[service]
    return IncidentRequest.model_validate(
        {
            "service": service,
            "environment": "production",
            "symptoms": symptoms,
            "time_range": {"start": f"2026-10-19T{start}:00Z", "end": f"2026-10-19T{end}:00Z"},
        }
    )


def investigate(service: str) -> IncidentReport:
    service_ = InvestigationService(DeterministicOrchestrator(), build_default_registry())
    return asyncio.run(service_.investigate(request_for(service)))


def statuses(report: IncidentReport) -> dict[str, str]:
    return {source.tool: source.status for source in report.data_sources}


def test_faulty_deployment_is_a_high_confidence_hypothesis_with_rollback() -> None:
    report = investigate("payments-api")

    [hypothesis] = report.hypotheses
    assert hypothesis.id == "h-1"
    assert hypothesis.confidence == "high"
    assert hypothesis.statement == "Deployment d-0042 of version 1.8.0 caused the abnormal signals."
    assert hypothesis.supporting_evidence_ids == [
        "deploy:payments-api:d-0042",
        "alarm:payments-api:a-pay-001",
        "log:payments-api:l-pay-001",
        "log:payments-api:l-pay-002",
    ]
    assert "4 minutes before the first abnormal signal" in hypothesis.rationale
    [proposal] = report.remediation_proposals
    assert proposal.action == "Roll back payments-api in production from version 1.8.0 to 1.7.3."
    assert proposal.hypothesis_id == "h-1"
    assert proposal.requires_human_approval is True
    assert report.summary.startswith(
        "Abnormal signals for payments-api in production began at 2026-10-19 10:09 UTC."
    )
    assert report.facts[0].evidence_ids == ["alarm:payments-api:a-pay-001"]
    assert [step.hypothesis_ids for step in report.next_steps][0] == ["h-1"]
    assert report.next_steps[-1].evidence_ids == ["runbook:payments-5xx:mitigation"]


def test_infrastructure_change_in_lookback_is_the_leading_hypothesis() -> None:
    report = investigate("checkout-api")

    [hypothesis] = report.hypotheses
    assert hypothesis.confidence == "high"
    assert hypothesis.supporting_evidence_ids[0] == "change:checkout-api:c-0502"
    assert "21 minutes before" in hypothesis.rationale
    # The onset is the error log at 14:01:30, which precedes the alarm.
    assert report.facts[0].evidence_ids == ["log:checkout-api:l-chk-001"]
    assert report.remediation_proposals[0].action.startswith(
        "Revert the change to security_group sg-example-checkout-db"
    )


def test_multiple_correlated_changes_are_medium_and_closest_first() -> None:
    report = investigate("orders-api")

    assert [(item.id, item.confidence) for item in report.hypotheses] == [
        ("h-1", "medium"),
        ("h-2", "medium"),
    ]
    assert report.hypotheses[0].supporting_evidence_ids[0] == "deploy:orders-api:d-0107"
    assert report.hypotheses[1].supporting_evidence_ids[0] == "change:orders-api:c-0503"
    assert "Other changes are also correlated." in report.hypotheses[0].rationale
    assert len(report.remediation_proposals) == 2


def test_signals_without_a_correlated_change_produce_no_hypothesis() -> None:
    report = investigate("cart-api")

    assert report.hypotheses == []
    assert report.remediation_proposals == []
    assert "deploy:cart-api:d-0700" in report.timeline  # 70 minutes before onset: not correlated
    assert any(
        item.description.startswith("No deployment or infrastructure change started within 30")
        for item in report.uncertainties
    )
    assert report.next_steps[0].action.startswith("Check dependencies, traffic volume")
    assert report.next_steps[0].evidence_ids == ["alarm:cart-api:a-cart-001"]


def test_stale_source_is_flagged_without_making_report_partial() -> None:
    report = investigate("billing-api")

    assert report.status == "complete"
    assert statuses(report)["cloudwatch_signals"] == "stale"
    assert any(
        item.description
        == "cloudwatch_signals data is only current to 2026-10-19 10:20 UTC, before the end of "
        "the time range."
        for item in report.uncertainties
    )
    assert report.summary.endswith("Some sources are stale.")


def test_contradictory_records_cap_confidence_and_block_remediation() -> None:
    report = investigate("auth-api")

    [hypothesis] = report.hypotheses
    assert hypothesis.confidence == "low"
    assert hypothesis.contradicting_evidence_ids == ["log:auth-api:l-auth-001"]
    assert "log:auth-api:l-auth-001" not in hypothesis.supporting_evidence_ids
    assert report.remediation_proposals == []
    contradiction = next(item for item in report.uncertainties if "disagree" in item.description)
    assert contradiction.evidence_ids == ["deploy:auth-api:d-0610", "log:auth-api:l-auth-001"]
    assert any(
        step.action.startswith("Confirm the actual state of deployment:d-0610")
        for step in report.next_steps
    )


def test_unavailable_change_source_is_reported_in_correlation_gap() -> None:
    report = investigate("inventory-api")

    assert report.status == "partial"
    assert report.summary.endswith("Some sources failed, so this assessment is incomplete.")
    assert any(
        item.description.endswith("but some change sources were unavailable.")
        for item in report.uncertainties
    )


def test_quiet_service_reports_no_abnormal_signals() -> None:
    report = investigate("notify-api")

    assert report.facts == []
    assert report.next_steps == []  # no runbook step without abnormal signals
    # Only a runbook section matched; with no events there is no signal gap to report.
    assert report.timeline == []
    assert "No data from cloudwatch_signals for this service and time range." in [
        item.description for item in report.uncertainties
    ]
    assert (
        report.summary
        == "No abnormal signals were found for notify-api in production in the time range."
    )


@pytest.mark.parametrize("service", sorted(SCENARIOS))
def test_every_claim_cites_evidence_from_the_report(service: str) -> None:
    report = investigate(service)
    evidence_ids = {item.source_id for item in report.evidence}

    for fact in report.facts:
        assert fact.evidence_ids and set(fact.evidence_ids) <= evidence_ids
    for hypothesis in report.hypotheses:
        cited = {*hypothesis.supporting_evidence_ids, *hypothesis.contradicting_evidence_ids}
        assert hypothesis.supporting_evidence_ids and cited <= evidence_ids
    for step in report.next_steps:
        assert set(step.evidence_ids) <= evidence_ids
    assert all(proposal.requires_human_approval for proposal in report.remediation_proposals)


@pytest.mark.parametrize("service", sorted(SCENARIOS))
def test_reports_are_deterministic(service: str) -> None:
    first = investigate(service).model_dump(mode="json")
    second = investigate(service).model_dump(mode="json")
    for report in (first, second):
        del report["investigation_id"], report["generated_at"]

    assert first == second


def evidence(source_id: str, kind: str, minute: int, tool: str, **attributes) -> Evidence:
    return Evidence(
        source_id=source_id,
        tool=tool,
        service="payments-api",
        environment="production",
        timestamp=datetime(2026, 10, 19, 10, minute, tzinfo=UTC),
        kind=kind,
        summary=f"Synthetic {kind}",
        attributes=attributes,
    )


AS_OF = datetime(2026, 10, 19, 23, 0, tzinfo=UTC)
SIGNALS = ToolResult(
    tool="cloudwatch_signals",
    status="ok",
    data_as_of=AS_OF,
    evidence=[
        evidence(
            "alarm:payments-api:a1",
            "alarm_state_change",
            10,
            "cloudwatch_signals",
            new_state="ALARM",
            alarm_name="a1",
        ),
        evidence("log:payments-api:l1", "log_event", 11, "cloudwatch_signals", level="ERROR"),
    ],
)


def deployment_result(status: str = "succeeded") -> ToolResult:
    return ToolResult(
        tool="deployment_history",
        status="ok",
        data_as_of=AS_OF,
        evidence=[
            evidence(
                "deploy:payments-api:d1",
                "deployment",
                5,
                "deployment_history",
                deployment_id="d1",
                version="2.0.0",
                previous_version="1.9.0",
                status=status,
                entity="deployment:d1",
                entity_state=status,
            )
        ],
    )


def test_missing_change_source_caps_confidence_at_medium() -> None:
    results = [
        SIGNALS,
        deployment_result(),
        ToolResult(tool="infrastructure_changes", status="unavailable", error="timed out"),
    ]

    findings = build_findings(request_for("payments-api"), results)

    assert findings.status == "partial"
    assert findings.hypotheses[0].confidence == "medium"
    assert "Some change sources were unavailable" in findings.hypotheses[0].rationale


def test_complete_evidence_allows_high_confidence() -> None:
    findings = build_findings(request_for("payments-api"), [SIGNALS, deployment_result()])

    assert findings.hypotheses[0].confidence == "high"


def test_failed_deployment_gets_no_rollback_proposal() -> None:
    findings = build_findings(request_for("payments-api"), [SIGNALS, deployment_result("failed")])

    assert findings.hypotheses
    assert findings.remediation_proposals == []


def test_all_sources_failing_returns_partial_report_without_findings() -> None:
    results = [
        ToolResult(tool="cloudwatch_signals", status="unavailable", error="timed out"),
        ToolResult(tool="deployment_history", status="malformed", error="output failed validation"),
    ]

    findings = build_findings(request_for("payments-api"), results)

    assert findings.status == "partial"
    assert findings.facts == findings.hypotheses == findings.remediation_proposals == []
    assert findings.summary == (
        "No evidence was collected for payments-api in production, so no findings could be made. "
        "Some sources failed, so this assessment is incomplete."
    )
    assert [item.tool for item in findings.uncertainties] == [
        "cloudwatch_signals",
        "deployment_history",
    ]
