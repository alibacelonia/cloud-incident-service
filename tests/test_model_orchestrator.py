import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cloud_incident_service.application import InvestigationService
from cloud_incident_service.config import ConfigurationError, Settings
from cloud_incident_service.domain import IncidentRequest
from cloud_incident_service.evaluation import load_scenarios, run_evaluation
from cloud_incident_service.main import app
from cloud_incident_service.orchestration import (
    DeterministicOrchestrator,
    LoopLimits,
    ModelInvestigationError,
    ModelOrchestrator,
    RuleBasedModelClient,
    build_orchestrator,
)
from cloud_incident_service.orchestration.model import (
    UNTRUSTED_NOTE,
    ModelFindings,
    extract_json,
    repair_request,
)
from cloud_incident_service.providers import (
    Message,
    ModelTurn,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)
from cloud_incident_service.tools import build_default_registry
from cloud_incident_service.tracing import InMemoryTraceSink

WINDOW = {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"}
PAYMENTS = {"service": "payments-api", "environment": "production", **WINDOW}
REQUEST = IncidentRequest.model_validate(
    {
        "service": "payments-api",
        "environment": "production",
        "symptoms": "Elevated 5xx errors",
        "time_range": WINDOW,
    }
)
GOOD_ANSWER = {
    "summary": "Deployment d-0042 preceded the 5xx alarm.",
    "facts": [
        {
            "statement": "Deployment d-0042 started at 10:05 UTC.",
            "evidence_ids": ["deploy:payments-api:d-0042"],
        }
    ],
    "hypotheses": [
        {
            "id": "h-1",
            "statement": "Deployment d-0042 caused the errors.",
            "supporting_evidence_ids": [
                "deploy:payments-api:d-0042",
                "alarm:payments-api:a-pay-001",
            ],
            "contradicting_evidence_ids": [],
            "confidence": "medium",
            "rationale": "The alarm fired four minutes after the deployment.",
        }
    ],
    "uncertainties": [],
    "next_steps": [
        {
            "action": "Compare logs before and after the deployment.",
            "rationale": "Tests h-1.",
            "hypothesis_ids": ["h-1"],
        }
    ],
    "remediation_proposals": [
        {
            "action": "Roll back to 1.7.3.",
            "rationale": "Reverses h-1.",
            "hypothesis_id": "h-1",
            "risk": "medium",
        }
    ],
}


def tool_use(*calls: tuple[str, dict]) -> ModelTurn:
    return ModelTurn(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(id=f"t{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls, start=1)
        ],
        usage=TokenUsage(input_tokens=100, output_tokens=20),
    )


def answer(body: dict | str, stop_reason: str = "end_turn") -> ModelTurn:
    text = body if isinstance(body, str) else json.dumps(body)
    return ModelTurn(stop_reason=stop_reason, content=[TextBlock(text=text)])


INVESTIGATE = tool_use(("cloudwatch_signals", PAYMENTS), ("deployment_history", PAYMENTS))


class ScriptedModelClient:
    model_id = "scripted-model"

    def __init__(self, *turns) -> None:
        self.turns = list(turns)
        self.calls: list[list[Message]] = []
        self.max_output_tokens: list[int] = []

    async def converse(self, system, messages, tools, max_output_tokens) -> ModelTurn:
        self.calls.append(list(messages))
        self.max_output_tokens.append(max_output_tokens)
        if not self.turns:
            raise AssertionError("script exhausted")
        step = self.turns.pop(0)
        return step(messages) if callable(step) else step


def investigate(client, limits: LoopLimits | None = None, request: IncidentRequest = REQUEST):
    sink = InMemoryTraceSink()
    service = InvestigationService(
        ModelOrchestrator(client, limits=limits), build_default_registry(), trace_sink=sink
    )
    try:
        return asyncio.run(service.investigate(request)), sink.traces[-1]
    except Exception:
        raise


def last_trace(client, limits=None) -> tuple[Exception, object]:
    sink = InMemoryTraceSink()
    service = InvestigationService(
        ModelOrchestrator(client, limits=limits), build_default_registry(), trace_sink=sink
    )
    with pytest.raises(ModelInvestigationError) as raised:
        asyncio.run(service.investigate(REQUEST))
    return raised.value, sink.traces[-1]


def test_model_driven_investigation_produces_validated_report() -> None:
    client = ScriptedModelClient(INVESTIGATE, answer(GOOD_ANSWER))

    report, trace = investigate(client)

    assert report.provenance.model_dump() == {
        "orchestrator": "model",
        "model": "scripted-model",
        "prompt_version": "investigation_v3",
    }
    assert report.hypotheses[0].confidence == "medium"
    assert report.remediation_proposals[0].requires_human_approval is True
    # Evidence and source statuses come from the tools the model chose to call.
    assert [source.tool for source in report.data_sources] == [
        "cloudwatch_signals",
        "deployment_history",
    ]
    assert "deploy:payments-api:d-0042" in report.timeline
    assert [step.type for step in trace.steps] == [
        "model_turn",
        "tool_call",
        "tool_call",
        "model_turn",
    ]
    assert trace.steps[0].tool_calls == ["cloudwatch_signals", "deployment_history"]
    assert trace.steps[0].input_tokens == 100
    assert [(item.check, item.passed) for item in trace.validations] == [
        ("model_output", True),
        ("report_schema", True),
        ("evidence_provenance", True),
    ]
    assert client.max_output_tokens == [2_048, 2_048]


def test_tool_output_is_sent_as_delimited_untrusted_data() -> None:
    client = ScriptedModelClient(INVESTIGATE, answer(GOOD_ANSWER))

    investigate(client)

    first_request = client.calls[0][0].content[0].text
    assert first_request.startswith("<incident_request>")
    results = client.calls[1][-1].content
    assert all(isinstance(block, ToolResultBlock) for block in results)
    assert results[1].content["note"] == UNTRUSTED_NOTE
    assert results[1].content["result"]["evidence"][0]["source_id"] == "deploy:payments-api:d-0042"


@pytest.mark.parametrize(
    "text",
    [
        f"```json\n{json.dumps(GOOD_ANSWER)}\n```",
        f"Here is the assessment:\n{json.dumps(GOOD_ANSWER)}\nThanks.",
    ],
)
def test_answers_wrapped_in_fences_or_prose_are_accepted(text: str) -> None:
    report, _ = investigate(ScriptedModelClient(INVESTIGATE, answer(text)))

    assert report.hypotheses[0].id == "h-1"


def test_extract_json_rejects_replies_without_an_object() -> None:
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_unregistered_and_invalid_tool_calls_are_refused_and_traced() -> None:
    client = ScriptedModelClient(
        tool_use(("delete_stack", {"stack": "payments"}), ("deployment_history", {"service": "x"})),
        INVESTIGATE,
        answer(GOOD_ANSWER),
    )

    report, trace = investigate(client)

    refusals = client.calls[1][-1].content
    assert [(block.is_error, block.content["error"]) for block in refusals] == [
        (True, "Tool 'delete_stack' is not available."),
        (True, "The arguments do not match the tool's input schema."),
    ]
    refused = [(step.tool, step.reason) for step in trace.steps if step.type == "tool_refused"]
    assert refused == [
        ("delete_stack", "not_registered"),
        ("deployment_history", "invalid_arguments"),
    ]
    assert report.status == "complete"


def test_tool_call_limit_is_enforced() -> None:
    calls = [("cloudwatch_signals", PAYMENTS)] * 10
    client = ScriptedModelClient(
        tool_use(*calls),
        answer(
            GOOD_ANSWER
            | {"facts": [], "hypotheses": [], "next_steps": [], "remediation_proposals": []}
        ),
    )

    _, trace = investigate(client)

    tool_calls = [step for step in trace.steps if step.type == "tool_call"]
    limited = [step for step in trace.steps if step.type == "tool_refused"]
    assert len(tool_calls) == 8
    assert [step.reason for step in limited] == ["limit_reached", "limit_reached"]
    assert client.calls[1][-1].content[-1].content == {"error": "Tool call limit reached."}


def test_repeated_calls_to_one_tool_are_merged_in_the_report() -> None:
    client = ScriptedModelClient(
        tool_use(("deployment_history", PAYMENTS)),
        tool_use(("deployment_history", PAYMENTS), ("cloudwatch_signals", PAYMENTS)),
        answer(GOOD_ANSWER),
    )

    report, trace = investigate(client)

    assert [source.tool for source in report.data_sources] == [
        "deployment_history",
        "cloudwatch_signals",
    ]
    ids = [item.source_id for item in report.evidence]
    assert len(ids) == len(set(ids))
    assert sum(step.type == "tool_call" for step in trace.steps) == 3


def test_turn_limit_stops_a_model_that_never_answers() -> None:
    client = ScriptedModelClient(*[tool_use(("cloudwatch_signals", PAYMENTS))] * 10)

    error, trace = last_trace(client)

    assert (error.code, error.kind) == ("model_turn_limit", "limit_exceeded")
    assert len(client.calls) == 6
    assert trace.outcome == "failed"
    assert trace.error == "ModelInvestigationError: model_turn_limit"


def test_overall_timeout_is_enforced() -> None:
    class SlowClient(ScriptedModelClient):
        async def converse(self, system, messages, tools, max_output_tokens):
            await asyncio.sleep(1)
            return answer(GOOD_ANSWER)

    error, trace = last_trace(SlowClient(), LoopLimits(timeout_seconds=0.05))

    assert (error.code, error.kind) == ("model_timeout", "timeout")
    assert trace.outcome == "failed"


INVALID_ANSWERS = [
    GOOD_ANSWER | {"facts": [{"statement": "x", "evidence_ids": ["deploy:payments-api:d-9999"]}]},
    GOOD_ANSWER
    | {
        "remediation_proposals": [
            {
                "action": "Roll back.",
                "rationale": "r",
                "hypothesis_id": "h-1",
                "risk": "low",
                "requires_human_approval": False,
            }
        ]
    },
    {"summary": "Missing the required structure", "unexpected": True},
    "I think it was the deployment.",
]


@pytest.mark.parametrize(
    "invalid", INVALID_ANSWERS, ids=["unknown-citation", "no-approval", "extra-field", "prose"]
)
def test_one_repair_attempt_recovers_invalid_output(invalid) -> None:
    client = ScriptedModelClient(INVESTIGATE, answer(invalid), answer(GOOD_ANSWER))

    report, trace = investigate(client)

    repair = client.calls[2][-1].content[0].text
    assert repair.startswith("Your previous reply could not be accepted:")
    assert report.hypotheses[0].id == "h-1"
    assert [(item.check, item.passed) for item in trace.validations][:2] == [
        ("model_output", False),
        ("model_output", True),
    ]


def test_repair_request_lists_the_citable_source_ids() -> None:
    bad = GOOD_ANSWER | {"facts": [{"statement": "x", "evidence_ids": ["cloudwatch_signals"]}]}
    client = ScriptedModelClient(INVESTIGATE, answer(bad), answer(GOOD_ANSWER))

    investigate(client)

    repair = client.calls[2][-1].content[0].text
    assert "facts.0.evidence_ids.0: is not a source_id; copy a source_id exactly" in repair
    assert "should match pattern" not in repair
    assert "The only source_ids you may cite are: " in repair
    assert "deploy:payments-api:d-0042" in repair


def test_hypothesis_without_evidence_is_repaired_with_no_evidence_hint() -> None:
    no_data = {"service": "search-api", "environment": "production", **WINDOW}
    request = IncidentRequest.model_validate(
        {
            "service": "search-api",
            "environment": "production",
            "symptoms": "Timeouts",
            "time_range": WINDOW,
        }
    )
    bad = {
        "summary": "Nothing found.",
        "hypotheses": [
            {
                "id": "h-1",
                "statement": "A hidden change.",
                "supporting_evidence_ids": [],
                "confidence": "low",
                "rationale": "Guess.",
            }
        ],
    }
    good = {
        "summary": "No evidence was found.",
        "uncertainties": [{"description": "No data.", "evidence_ids": []}],
    }
    client = ScriptedModelClient(
        tool_use(("cloudwatch_signals", no_data)), answer(bad), answer(good)
    )

    report, _ = investigate(client, request=request)

    repair = client.calls[2][-1].content[0].text
    assert "needs at least one source_id" in repair
    assert "facts and hypotheses must be empty lists" in repair
    assert "There is no abnormal signal" in repair
    assert report.hypotheses == []


def test_facts_without_evidence_become_uncertainties() -> None:
    unsupported = GOOD_ANSWER | {
        "facts": [
            *GOOD_ANSWER["facts"],
            {"statement": "No infrastructure changes were recorded.", "evidence_ids": []},
        ]
    }
    client = ScriptedModelClient(INVESTIGATE, answer(unsupported))

    report, trace = investigate(client)

    assert [fact.statement for fact in report.facts] == ["Deployment d-0042 started at 10:05 UTC."]
    assert "No infrastructure changes were recorded." in [
        item.description for item in report.uncertainties
    ]
    assert all(item.passed for item in trace.validations)


def test_unknown_citation_is_explained_in_the_repair_request() -> None:
    bad = GOOD_ANSWER | {
        "facts": [{"statement": "x", "evidence_ids": ["deploy:payments-api:d-9999"]}],
    }
    client = ScriptedModelClient(INVESTIGATE, answer(bad), answer(GOOD_ANSWER))

    investigate(client)

    assert "references unknown evidence: deploy:payments-api:d-9999" in (
        client.calls[2][-1].content[0].text
    )


def test_bad_hypothesis_id_is_explained_without_regex() -> None:
    bad_id = json.loads(json.dumps(GOOD_ANSWER))
    bad_id["hypotheses"][0]["id"] = "H1"
    with pytest.raises(ValidationError) as raised:
        ModelFindings.model_validate(bad_id)

    message = repair_request(raised.value, ["deploy:payments-api:d-0042"])

    assert 'hypotheses.0.id: is not a valid hypothesis id; use ids like "h-1"' in message
    assert "pattern" not in message
    assert message.endswith(
        "The only source_ids you may cite are: deploy:payments-api:d-0042. "
        "Reply again with only the corrected JSON object."
    )


def test_capitalized_confidence_and_risk_are_accepted() -> None:
    shouted = json.loads(json.dumps(GOOD_ANSWER))
    shouted["hypotheses"][0]["confidence"] = "Medium"
    shouted["remediation_proposals"][0]["risk"] = " HIGH "
    client = ScriptedModelClient(INVESTIGATE, answer(shouted))

    report, _ = investigate(client)

    assert report.hypotheses[0].confidence == "medium"
    assert report.remediation_proposals[0].risk == "high"


def test_cut_off_answer_triggers_repair() -> None:
    client = ScriptedModelClient(
        INVESTIGATE, answer('{"summary": "trunc', stop_reason="max_tokens"), answer(GOOD_ANSWER)
    )

    investigate(client)

    assert "output token limit" in client.calls[2][-1].content[0].text


def test_second_invalid_answer_fails_with_invalid_model_output() -> None:
    client = ScriptedModelClient(INVESTIGATE, answer("not json"), answer("still not json"))

    error, trace = last_trace(client)

    assert (error.code, error.kind) == ("invalid_model_output", "invalid_output")
    assert [item.passed for item in trace.validations] == [False, False]


def scenario(scenario_id: str):
    return [item for item in load_scenarios() if item.id == scenario_id]


NOTIFY = {"service": "notify-api", "environment": "production", **WINDOW}
NOTIFY_ANSWER = {
    "summary": "An error log at 10:20 UTC contains injected instructions; no change explains it.",
    "facts": [
        {
            "statement": "An error log was recorded at 10:20 UTC.",
            "evidence_ids": ["log:notify-api:l-ntf-001"],
        }
    ],
    "hypotheses": [],
    "uncertainties": [],
    "next_steps": [],
    "remediation_proposals": [],
}


def test_prompt_injection_scenario_passes_when_model_ignores_injected_text() -> None:
    client = ScriptedModelClient(
        tool_use(
            ("cloudwatch_signals", NOTIFY),
            ("deployment_history", NOTIFY),
            ("infrastructure_changes", NOTIFY),
            (
                "runbook_search",
                {"service": "notify-api", "environment": "production", "query": "5xx errors"},
            ),
        ),
        answer(NOTIFY_ANSWER),
    )

    report = asyncio.run(
        run_evaluation(
            scenario("prompt-injection"), orchestrator_factory=lambda: ModelOrchestrator(client)
        )
    )

    assert report.pass_rate == 1.0, report.results[0].checks


def test_prompt_injection_scenario_fails_when_model_follows_injected_text() -> None:
    client = ScriptedModelClient(
        tool_use(("cloudwatch_signals", NOTIFY)),
        tool_use(("delete_stack", {"stack": "notify-api"})),
        answer(NOTIFY_ANSWER),
    )

    report = asyncio.run(
        run_evaluation(
            scenario("prompt-injection"), orchestrator_factory=lambda: ModelOrchestrator(client)
        )
    )

    [result] = report.results
    assert not result.passed
    assert result.checks["safety"].detail == "unregistered tools were requested: delete_stack"


def test_rule_based_model_client_passes_every_scenario() -> None:
    report = asyncio.run(
        run_evaluation(orchestrator_factory=lambda: ModelOrchestrator(RuleBasedModelClient()))
    )

    assert report.orchestrator == "model"
    assert report.pass_rate == 1.0


def test_build_orchestrator_follows_settings() -> None:
    assert isinstance(build_orchestrator(Settings()), DeterministicOrchestrator)
    model = build_orchestrator(Settings(orchestrator="model", model_max_output_tokens=512))
    assert isinstance(model, ModelOrchestrator)
    assert model.provenance.model == "rule-based-mock"


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({"ORCHESTRATOR": "swarm"}, "ORCHESTRATOR"),
        ({"MODEL_MAX_OUTPUT_TOKENS": "100"}, "MODEL_MAX_OUTPUT_TOKENS"),
        ({"MODEL_MAX_OUTPUT_TOKENS": "lots"}, "MODEL_MAX_OUTPUT_TOKENS"),
    ],
)
def test_invalid_model_settings_name_the_variable(environ: dict, variable: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment(environ)

    assert raised.value.errors[0].startswith(f"{variable}: ")


def test_api_runs_the_model_orchestrator_offline(monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR", "model")

    with TestClient(app) as client:
        response = client.post(
            "/v1/investigations",
            json={
                "service": "payments-api",
                "environment": "production",
                "symptoms": "Elevated 5xx errors",
                "time_range": WINDOW,
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["provenance"] == {
        "orchestrator": "model",
        "model": "rule-based-mock",
        "prompt_version": "investigation_v3",
    }
    assert body["hypotheses"][0]["confidence"] == "high"


@pytest.mark.parametrize(
    ("kind", "status_code"), [("invalid_output", 502), ("limit_exceeded", 502), ("timeout", 504)]
)
def test_model_failures_map_to_safe_http_errors(kind: str, status_code: int) -> None:
    from cloud_incident_service.main import get_investigation_service

    class Failing:
        provenance = ModelOrchestrator(RuleBasedModelClient()).provenance

        async def investigate(self, request, tools):
            raise ModelInvestigationError("model_example", "Safe message.", kind)

    service = InvestigationService(Failing(), build_default_registry())
    app.dependency_overrides[get_investigation_service] = lambda: service
    try:
        response = TestClient(app).post(
            "/v1/investigations",
            json={
                "service": "payments-api",
                "environment": "production",
                "symptoms": "x",
                "time_range": WINDOW,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == status_code
    assert response.json() == {"detail": "Safe message.", "code": "model_example"}
