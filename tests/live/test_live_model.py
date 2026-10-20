"""Live model tests. Read-only (model invocation only), bounded, and safe to rerun.

Bedrock:
    RUN_LIVE_TESTS=1 AI_MODEL=<model-id> AWS_REGION=<region> uv run pytest -m live -s
Anthropic API:
    RUN_LIVE_TESTS=1 AI_PROVIDER=anthropic AI_MODEL=<model-id> ANTHROPIC_API_KEY=<key> \
        uv run pytest -m live -s
"""

import asyncio

import pytest
from live_support import (
    EVALUATION_TIMEOUT_SECONDS,
    INVESTIGATION_MAX_OUTPUT_TOKENS,
    LIVE_PASS_THRESHOLD,
    SMOKE_MAX_OUTPUT_TOKENS,
    SMOKE_TIMEOUT_SECONDS,
)

from cloud_incident_service.application import InvestigationService
from cloud_incident_service.evaluation import load_scenarios, run_evaluation
from cloud_incident_service.evaluation.__main__ import format_table
from cloud_incident_service.orchestration import ModelOrchestrator
from cloud_incident_service.providers import Message, TextBlock, create_model_client
from cloud_incident_service.tools import build_default_registry
from cloud_incident_service.tracing import InMemoryTraceSink

pytestmark = pytest.mark.live


def client_for(settings):
    return create_model_client(settings)


def orchestrator_for(settings) -> ModelOrchestrator:
    return ModelOrchestrator(
        client_for(settings), max_output_tokens=INVESTIGATION_MAX_OUTPUT_TOKENS
    )


def test_converse_smoke(live_model_settings) -> None:
    client = client_for(live_model_settings)
    messages = [Message(role="user", content=[TextBlock(text="Reply with the single word OK.")])]

    turn = asyncio.run(
        asyncio.wait_for(
            client.converse("You are a test endpoint.", messages, [], SMOKE_MAX_OUTPUT_TOKENS),
            SMOKE_TIMEOUT_SECONDS,
        )
    )

    assert turn.text.strip()
    assert turn.usage.input_tokens > 0
    assert turn.usage.output_tokens <= SMOKE_MAX_OUTPUT_TOKENS


def test_live_investigation_uses_tools_and_returns_a_valid_report(live_model_settings) -> None:
    scenario = next(item for item in load_scenarios() if item.id == "faulty-deployment")
    sink = InMemoryTraceSink()
    service = InvestigationService(
        orchestrator_for(live_model_settings), build_default_registry(), trace_sink=sink
    )

    report = asyncio.run(service.investigate(scenario.request))

    trace = sink.traces[-1]
    assert report.provenance.model == live_model_settings.model
    assert any(step.type == "tool_call" for step in trace.steps)
    assert not any(
        step.type == "tool_refused" and step.reason == "not_registered" for step in trace.steps
    )
    assert all(item.requires_human_approval for item in report.remediation_proposals)
    tokens = sum(step.output_tokens for step in trace.steps if step.type == "model_turn")
    print(f"\nlive investigation used {tokens} output tokens over {len(trace.steps)} steps")


def test_live_evaluation_meets_the_pass_threshold(live_model_settings) -> None:
    report = asyncio.run(
        asyncio.wait_for(
            run_evaluation(orchestrator_factory=lambda: orchestrator_for(live_model_settings)),
            EVALUATION_TIMEOUT_SECONDS,
        )
    )

    print("\n" + format_table(report))
    assert report.pass_rate >= LIVE_PASS_THRESHOLD, (
        f"live pass rate {report.pass_rate:.0%} is below {LIVE_PASS_THRESHOLD:.0%}"
    )
