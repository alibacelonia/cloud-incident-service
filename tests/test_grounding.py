"""Model answers must follow the correlation rules (ADR-015).

Each scripted answer below reproduces a failure seen in the first live runs with Claude 3 Haiku.
"""

import asyncio
import json

import pytest

from cloud_incident_service.evaluation import load_scenarios, run_evaluation
from cloud_incident_service.orchestration import ModelOrchestrator
from cloud_incident_service.providers import ModelTurn, TextBlock, ToolUseBlock


class ScriptedModel:
    model_id = "scripted-model"

    def __init__(self, *answers: dict) -> None:
        self.answers = list(answers)
        self.requests: list[str] = []

    async def converse(self, system, messages, tools, max_output_tokens) -> ModelTurn:
        last = messages[-1].content[0]
        if isinstance(last, TextBlock):
            self.requests.append(last.text)
        if len(messages) == 1:
            scenario_request = json.loads(
                last.text.split("\n", 1)[1].rsplit("</incident_request>", 1)[0]
            )
            scope = {"service": scenario_request["service"], "environment": "production"}
            window = {**scope, **scenario_request["time_range"]}
            calls = [
                ("cloudwatch_signals", window),
                ("deployment_history", window),
                ("infrastructure_changes", window),
            ]
            return ModelTurn(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(id=f"t{index}", name=name, arguments=arguments)
                    for index, (name, arguments) in enumerate(calls)
                ],
            )
        return ModelTurn(
            stop_reason="end_turn", content=[TextBlock(text=json.dumps(self.answers.pop(0)))]
        )


def evaluate(scenario_id: str, model: ScriptedModel):
    scenarios = [item for item in load_scenarios() if item.id == scenario_id]
    report = asyncio.run(
        run_evaluation(scenarios, orchestrator_factory=lambda: ModelOrchestrator(model))
    )
    return report.results[0]


def hypothesis(hid: str, support: list[str], confidence: str, contradicting=()) -> dict:
    return {
        "id": hid,
        "statement": f"Cause {hid}.",
        "supporting_evidence_ids": support,
        "contradicting_evidence_ids": list(contradicting),
        "confidence": confidence,
        "rationale": "Timing.",
    }


def rollback(hid: str) -> dict:
    return {
        "action": "Roll back.",
        "rationale": "Reverses it.",
        "hypothesis_id": hid,
        "risk": "medium",
    }


QUIET = {
    "summary": "No correlated change.",
    "uncertainties": [{"description": "No change preceded the signals.", "evidence_ids": []}],
}


def test_uncorrelated_deployment_is_rejected_then_repaired() -> None:
    # Live failure: blamed a deployment that started 70 minutes before the first signal.
    blamed = {
        "summary": "Deployment d-0700 caused it.",
        "facts": [{"statement": "Latency alarm.", "evidence_ids": ["alarm:cart-api:a-cart-001"]}],
        "hypotheses": [
            hypothesis("h-1", ["deploy:cart-api:d-0700", "alarm:cart-api:a-cart-001"], "medium")
        ],
        "remediation_proposals": [rollback("h-1")],
    }
    fixed = QUIET | {"facts": blamed["facts"]}
    model = ScriptedModel(blamed, fixed)

    result = evaluate("no-correlated-change", model)

    repair = model.requests[-1]
    assert "h-1 cites deploy:cart-api:d-0700, which did not start within the 30 minutes" in repair
    assert (
        "The first abnormal signal is alarm:cart-api:a-cart-001 at 2026-10-19 12:10 UTC." in repair
    )
    assert "No change started within the 30 minutes before it" in repair
    assert result.passed, result.checks


def test_hypothesis_without_a_correlated_change_is_rejected() -> None:
    # Live failure: guessed a dependency cause and proposed remediation with no change to blame.
    guessed = {
        "summary": "Upstream outage.",
        "facts": [{"statement": "Error log.", "evidence_ids": ["log:notify-api:l-ntf-001"]}],
        "hypotheses": [hypothesis("h-1", ["log:notify-api:l-ntf-001"], "medium")],
        "remediation_proposals": [rollback("h-1")],
    }
    model = ScriptedModel(guessed, QUIET | {"facts": guessed["facts"]})

    result = evaluate("prompt-injection", model)

    assert "h-1 must cite a correlated change in supporting_evidence_ids" in model.requests[-1]
    assert result.passed, result.checks


def test_high_confidence_with_two_candidates_is_rejected_and_hypotheses_are_ordered() -> None:
    # Live failure: rated one of two correlated changes high and led with the earlier change.
    both = ["deploy:orders-api:d-0107", "change:orders-api:c-0503"]
    over_sure = {
        "summary": "Capacity change.",
        "hypotheses": [
            hypothesis("h-1", ["change:orders-api:c-0503", "alarm:orders-api:a-ord-001"], "high"),
            hypothesis("h-2", ["deploy:orders-api:d-0107", "alarm:orders-api:a-ord-001"], "medium"),
        ],
    }
    corrected = json.loads(json.dumps(over_sure))
    corrected["hypotheses"][0]["confidence"] = "medium"
    model = ScriptedModel(over_sure, corrected)

    result = evaluate("multiple-changes", model)

    repair = model.requests[-1]
    assert "h-1 cannot be high: more than one change is correlated" in repair
    assert all(source_id in repair for source_id in both)
    # Equal confidence: the change closest to the onset (d-0107 at 10:12) leads.
    assert result.leading_hypothesis == "deployment"
    assert result.passed, result.checks


def test_ignored_contradiction_is_rejected_then_repaired() -> None:
    # Live failure: ignored the rolled-back log, rated high, and proposed a rollback.
    support = ["deploy:auth-api:d-0610", "alarm:auth-api:a-auth-001"]
    ignored = {
        "summary": "Deployment d-0610.",
        "facts": [{"statement": "Deployed.", "evidence_ids": ["deploy:auth-api:d-0610"]}],
        "hypotheses": [hypothesis("h-1", support, "high")],
        "remediation_proposals": [rollback("h-1")],
    }
    fixed = {
        "summary": "Deployment d-0610, but records disagree.",
        "facts": ignored["facts"]
        + [{"statement": "Rolled back.", "evidence_ids": ["log:auth-api:l-auth-001"]}],
        "hypotheses": [hypothesis("h-1", support, "low", ["log:auth-api:l-auth-001"])],
    }
    model = ScriptedModel(ignored, fixed)

    result = evaluate("contradictory-records", model)

    assert "h-1 is contradicted by log:auth-api:l-auth-001" in model.requests[-1]
    assert result.passed, result.checks


def test_remediation_for_a_low_hypothesis_is_rejected() -> None:
    support = ["deploy:auth-api:d-0610", "alarm:auth-api:a-auth-001"]
    answer = {
        "summary": "Deployment d-0610.",
        "hypotheses": [hypothesis("h-1", support, "low", ["log:auth-api:l-auth-001"])],
        "remediation_proposals": [rollback("h-1")],
    }
    model = ScriptedModel(answer, answer)

    result = evaluate("contradictory-records", model)

    assert "remediation for h-1 is not allowed because it is low confidence" in model.requests[-1]
    assert not result.passed
    assert "invalid_model_output" in result.checks["schema"].detail


@pytest.mark.parametrize(
    ("scenario_id", "cited"),
    [
        ("stale-data", ["alarm:billing-api:a-bill-001"]),
        ("unavailable-source", ["alarm:inventory-api:a-inv-001"]),
        ("malformed-source", []),
    ],
)
def test_quiet_answers_pass_scenarios_without_correlated_changes(
    scenario_id: str, cited: list[str]
) -> None:
    facts = [{"statement": "An alarm fired.", "evidence_ids": cited}] if cited else []
    model = ScriptedModel(QUIET | {"facts": facts})

    result = evaluate(scenario_id, model)

    assert result.passed, result.checks


def test_no_data_answer_with_ungrounded_next_steps_passes() -> None:
    # Live failure: with no evidence at all, the model suggested next steps it could not cite.
    answer = {
        "summary": "No source returned data for search-api.",
        "uncertainties": [{"description": "No data from any source.", "evidence_ids": []}],
        "next_steps": [
            {"action": "Confirm that search-api publishes metrics.", "rationale": "No data."}
        ],
    }

    result = evaluate("no-data", ScriptedModel(answer))

    assert result.passed, result.checks


def test_unverified_steps_still_count_for_no_correlated_change() -> None:
    answer = QUIET | {
        "facts": [{"statement": "Latency alarm.", "evidence_ids": ["alarm:cart-api:a-cart-001"]}],
        "next_steps": [
            {"action": "Check pricing-service health.", "rationale": "Upstream 503s."},
            {
                "action": "Review the latency alarm.",
                "rationale": "First signal.",
                "evidence_ids": ["alarm:cart-api:a-cart-001"],
            },
        ],
    }

    result = evaluate("no-correlated-change", ScriptedModel(answer))

    assert result.passed, result.checks
