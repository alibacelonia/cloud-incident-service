from copy import deepcopy

import pytest
from pydantic import ValidationError

from cloud_incident_service.domain import IncidentReport

DEPLOY = "deploy:payments-api:d-0042"
ALARM = "alarm:payments-api:5xx-high"


def valid_report() -> dict:
    """A small but complete report that satisfies every FR-5 rule."""
    return {
        "investigation_id": "00000000-0000-4000-8000-000000000001",
        "generated_at": "2026-10-19T11:05:00Z",
        "request": {
            "service": "payments-api",
            "environment": "production",
            "symptoms": "Elevated 5xx errors",
            "time_range": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"},
        },
        "provenance": {"orchestrator": "deterministic", "model": "deterministic"},
        "status": "complete",
        "summary": "5xx errors began four minutes after deployment d-0042.",
        "evidence": [
            {
                "source_id": ALARM,
                "tool": "cloudwatch_signals",
                "service": "payments-api",
                "environment": "production",
                "timestamp": "2026-10-19T10:09:00Z",
                "kind": "alarm_state_change",
                "summary": "5xx alarm entered ALARM",
            },
            {
                "source_id": DEPLOY,
                "tool": "deployment_history",
                "service": "payments-api",
                "environment": "production",
                "timestamp": "2026-10-19T10:05:00Z",
                "kind": "deployment",
                "summary": "Deployment d-0042 of version 1.8.0 succeeded",
                "attributes": {"version": "1.8.0"},
            },
        ],
        "timeline": [DEPLOY, ALARM],
        "facts": [{"statement": "Deployment d-0042 finished at 10:05.", "evidence_ids": [DEPLOY]}],
        "hypotheses": [
            {
                "id": "h-1",
                "statement": "Version 1.8.0 introduced the errors.",
                "supporting_evidence_ids": [DEPLOY, ALARM],
                "confidence": "medium",
                "rationale": "The alarm followed the deployment within the correlation window.",
            }
        ],
        "uncertainties": [{"description": "No runbook matched.", "tool": "runbook_search"}],
        "next_steps": [
            {
                "action": "Compare error logs before and after 10:05.",
                "rationale": "Confirms whether errors start with the new version.",
                "hypothesis_ids": ["h-1"],
            }
        ],
        "remediation_proposals": [
            {
                "action": "Roll back to version 1.7.3.",
                "rationale": "Removes the suspected change.",
                "hypothesis_id": "h-1",
                "risk": "medium",
                "requires_human_approval": True,
            }
        ],
        "data_sources": [
            {"tool": "cloudwatch_signals", "status": "ok"},
            {"tool": "deployment_history", "status": "ok"},
            {"tool": "runbook_search", "status": "empty"},
        ],
    }


def assert_rejected(report: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        IncidentReport.model_validate(report)


def test_valid_report_passes() -> None:
    report = IncidentReport.model_validate(valid_report())

    assert report.remediation_proposals[0].requires_human_approval is True


def test_fact_without_evidence_is_rejected() -> None:
    report = valid_report()
    report["facts"][0]["evidence_ids"] = []
    assert_rejected(report, "evidence_ids")


def test_fact_citing_unknown_evidence_is_rejected() -> None:
    report = valid_report()
    report["facts"][0]["evidence_ids"] = ["deploy:payments-api:d-9999"]
    assert_rejected(report, "references unknown evidence: deploy:payments-api:d-9999")


def test_hypothesis_without_supporting_evidence_is_rejected() -> None:
    report = valid_report()
    report["hypotheses"][0]["supporting_evidence_ids"] = []
    assert_rejected(report, "supporting_evidence_ids")


@pytest.mark.parametrize("field", ["supporting_evidence_ids", "contradicting_evidence_ids"])
def test_hypothesis_citing_unknown_evidence_is_rejected(field: str) -> None:
    report = valid_report()
    report["hypotheses"][0][field] = ["log:payments-api:missing"]
    if field == "contradicting_evidence_ids":
        report["hypotheses"][0]["confidence"] = "low"
    assert_rejected(report, "references unknown evidence")


def test_contradicted_hypothesis_cannot_be_high_confidence() -> None:
    report = valid_report()
    report["hypotheses"][0].update(
        supporting_evidence_ids=[DEPLOY], contradicting_evidence_ids=[ALARM], confidence="high"
    )
    assert_rejected(report, "cannot be high confidence")


def test_evidence_cannot_support_and_contradict() -> None:
    report = valid_report()
    report["hypotheses"][0].update(contradicting_evidence_ids=[DEPLOY], confidence="low")
    assert_rejected(report, "both support and contradict")


def test_remediation_must_require_human_approval() -> None:
    report = valid_report()
    report["remediation_proposals"][0]["requires_human_approval"] = False
    assert_rejected(report, "requires_human_approval")


def test_remediation_referencing_unknown_hypothesis_is_rejected() -> None:
    report = valid_report()
    report["remediation_proposals"][0]["hypothesis_id"] = "h-9"
    assert_rejected(report, "references unknown hypotheses: h-9")


def test_next_step_must_reference_evidence_or_hypothesis() -> None:
    report = valid_report()
    report["next_steps"][0]["hypothesis_ids"] = []
    assert_rejected(report, "must reference evidence or a hypothesis")


def test_uncertainty_citing_unknown_evidence_is_rejected() -> None:
    report = valid_report()
    report["uncertainties"][0]["evidence_ids"] = ["log:payments-api:missing"]
    assert_rejected(report, "references unknown evidence")


def test_duplicate_evidence_ids_are_rejected() -> None:
    report = valid_report()
    report["evidence"].append(deepcopy(report["evidence"][0]))
    assert_rejected(report, "duplicate evidence source_id")


def test_duplicate_hypothesis_ids_are_rejected() -> None:
    report = valid_report()
    report["hypotheses"].append(deepcopy(report["hypotheses"][0]))
    assert_rejected(report, "duplicate hypothesis id")


def test_timeline_must_be_ordered() -> None:
    report = valid_report()
    report["timeline"] = [ALARM, DEPLOY]
    assert_rejected(report, "timeline must be ordered")


def test_timeline_citing_unknown_evidence_is_rejected() -> None:
    report = valid_report()
    report["timeline"].append("log:payments-api:missing")
    assert_rejected(report, "references unknown evidence")


@pytest.mark.parametrize(
    ("field", "value"), [("service", "orders-api"), ("environment", "staging")]
)
def test_evidence_outside_requested_scope_is_rejected(field: str, value: str) -> None:
    report = valid_report()
    report["evidence"][0][field] = value
    assert_rejected(report, "evidence outside the requested service")


@pytest.mark.parametrize("status", ["unavailable", "malformed"])
def test_complete_report_cannot_have_failed_sources(status: str) -> None:
    report = valid_report()
    report["data_sources"][0]["status"] = status
    assert_rejected(report, "report must be partial")


def test_complete_report_requires_a_data_source() -> None:
    report = valid_report()
    report["data_sources"] = []
    assert_rejected(report, "requires at least one data source")


def test_partial_report_with_failed_source_is_accepted() -> None:
    report = valid_report()
    report["status"] = "partial"
    report["data_sources"][0]["status"] = "unavailable"

    IncidentReport.model_validate(report)


def test_duplicate_data_source_tools_are_rejected() -> None:
    report = valid_report()
    report["data_sources"].append({"tool": "runbook_search", "status": "ok"})
    assert_rejected(report, "duplicate data source tool")


def test_unknown_report_fields_are_rejected() -> None:
    report = valid_report()
    report["confirmed_root_cause"] = "anything"
    assert_rejected(report, "confirmed_root_cause")
