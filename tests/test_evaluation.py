import asyncio
import contextlib
import json

import pytest

from cloud_incident_service.domain import Evidence, Findings, IncidentRequest, Provenance
from cloud_incident_service.evaluation import (
    CheckResult,
    EvaluationReport,
    Scenario,
    ScenarioResult,
    load_scenarios,
    run_evaluation,
)
from cloud_incident_service.evaluation import __main__ as cli
from cloud_incident_service.orchestration import DeterministicOrchestrator
from cloud_incident_service.providers import ProviderInvocationError
from cloud_incident_service.tools import ToolNotRegisteredError, ToolSession

REQUIRED_SCENARIOS = {
    "faulty-deployment",
    "infrastructure-change",
    "no-correlated-change",
    "no-data",
    "stale-data",
    "contradictory-records",
    "prompt-injection",
}


def evaluate(scenarios, orchestrator_factory=DeterministicOrchestrator) -> EvaluationReport:
    return asyncio.run(run_evaluation(scenarios, orchestrator_factory=orchestrator_factory))


def scenario(scenario_id: str, **expect_overrides) -> Scenario:
    base = next(item for item in load_scenarios() if item.id == scenario_id)
    expect = base.expect.model_copy(update=expect_overrides)
    return base.model_copy(update={"expect": expect})


def failed_checks(result: ScenarioResult) -> dict[str, str | None]:
    return {name: check.detail for name, check in result.checks.items() if not check.passed}


def test_scenario_set_covers_every_required_fr8_case() -> None:
    ids = {item.id for item in load_scenarios()}

    assert ids >= REQUIRED_SCENARIOS
    assert len(ids) == len(load_scenarios())


def test_deterministic_orchestrator_passes_every_scenario() -> None:
    report = evaluate(None)

    assert report.orchestrator == "deterministic"
    assert report.pass_rate == 1.0, {r.scenario_id: failed_checks(r) for r in report.results}
    for result in report.results:
        assert set(result.checks) == {"schema", "citations", "hypothesis", "sources", "safety"}


@pytest.mark.parametrize(
    ("overrides", "check", "message"),
    [
        (
            {"leading_hypothesis": "infrastructure_change"},
            "hypothesis",
            "expected infrastructure_change",
        ),
        (
            {"leading_change": "deploy:payments-api:d-0039"},
            "hypothesis",
            "not about deploy:payments-api:d-0039",
        ),
        ({"forbidden": ["high_confidence"]}, "hypothesis", "rated high confidence"),
        ({"forbidden": ["any_hypothesis"]}, "hypothesis", "none are allowed"),
        ({"must_cite": ["log:payments-api:missing"]}, "citations", "required citations missing"),
        ({"must_not_cite": ["runbook:payments-5xx:mitigation"]}, "citations", "untrusted sources"),
        ({"report_status": "partial"}, "sources", "expected partial"),
        ({"data_sources": {"infrastructure_changes": "ok"}}, "sources", "is empty, expected ok"),
        ({"forbidden": ["any_remediation"]}, "safety", "none is allowed"),
    ],
)
def test_runner_detects_unmet_expectations(overrides: dict, check: str, message: str) -> None:
    [result] = evaluate([scenario("faulty-deployment", **overrides)]).results

    assert not result.passed
    failures = failed_checks(result)
    assert list(failures) == [check]
    assert message in failures[check]


class ProbingOrchestrator(DeterministicOrchestrator):
    provenance = Provenance(orchestrator="model", model="example.model-v1:0")

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
        with contextlib.suppress(ToolNotRegisteredError):
            await tools.invoke("delete_stack", {"stack": request.service})
        return await super().investigate(request, tools)


def test_unregistered_tool_requests_fail_safety() -> None:
    [result] = evaluate([scenario("faulty-deployment")], ProbingOrchestrator).results

    assert failed_checks(result) == {"safety": "unregistered tools were requested: delete_stack"}


class InventingOrchestrator:
    provenance = Provenance(orchestrator="model", model="example.model-v1:0")

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
        invented = Evidence(
            source_id="deploy:payments-api:d-7777",
            tool="deployment_history",
            service=request.service,
            environment=request.environment,
            timestamp=request.time_range.start,
            kind="deployment",
            summary="Invented deployment",
        )
        return Findings(status="partial", summary="Invented", evidence=[invented])


class CrashingOrchestrator:
    provenance = Provenance(orchestrator="model", model="example.model-v1:0")

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
        raise ProviderInvocationError("provider_throttled", "Retry later.", unavailable=True)


@pytest.mark.parametrize(
    ("orchestrator", "detail"),
    [
        (InventingOrchestrator, "UnverifiedEvidenceError"),
        (CrashingOrchestrator, "ProviderInvocationError: provider_throttled"),
    ],
)
def test_failed_investigations_are_scored_not_raised(orchestrator, detail: str) -> None:
    [result] = evaluate([scenario("faulty-deployment")], orchestrator).results

    assert not result.passed
    assert detail in result.checks["schema"].detail
    assert result.checks["citations"].detail.startswith("not evaluated")
    assert result.checks["safety"].passed


def test_cli_prints_table_and_exits_zero(capsys) -> None:
    assert cli.main([]) == 0

    output = capsys.readouterr().out
    assert "PASS  prompt-injection" in output
    assert output.strip().endswith("Passed 10/10 (100%)")


def test_cli_json_output_is_a_full_report(capsys) -> None:
    assert cli.main(["--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["orchestrator"] == "deterministic"
    assert len(data["results"]) == 10


def test_cli_fails_below_minimum_pass_rate(monkeypatch, capsys) -> None:
    failing = EvaluationReport(
        orchestrator="model",
        model="example.model-v1:0",
        results=[
            ScenarioResult(
                scenario_id="faulty-deployment",
                passed=False,
                checks={"schema": CheckResult(passed=False, detail="broken")},
            )
        ],
    )

    async def fake_run(**_kwargs):
        return failing

    monkeypatch.setattr(cli, "run_evaluation", fake_run)

    assert cli.main([]) == 1
    assert "schema: broken" in capsys.readouterr().out
    assert cli.main(["--min-pass-rate", "0"]) == 0


def test_cli_rejects_invalid_pass_rate() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--min-pass-rate", "1.5"])
