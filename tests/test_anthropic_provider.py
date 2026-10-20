import asyncio
import logging
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from cloud_incident_service.config import ConfigurationError, Settings
from cloud_incident_service.providers import (
    Message,
    ProviderConfigurationError,
    ProviderInvocationError,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    create_model_client,
)
from cloud_incident_service.providers.anthropic import AnthropicModelClient

MODEL = "example-model"
API_KEY = "sk-ant-" + "k" * 40
# Raw SDK messages can echo request details; they must never reach callers.
SENSITIVE = "request req_0123456789 for org-example failed"


def response(content, stop_reason="end_turn", input_tokens=11, output_tokens=3):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def text(value: str):
    return SimpleNamespace(type="text", text=value)


def tool_use(block_id: str, name: str, arguments: dict):
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=arguments)


class FakeMessages:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class FakeAnthropic:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.messages = FakeMessages(result, error)


def converse(fake: FakeAnthropic, messages=None, tools=None, max_tokens: int = 512):
    client = AnthropicModelClient(MODEL, SecretStr(API_KEY), client=fake)
    messages = messages or [Message(role="user", content=[TextBlock(text="prompt")])]
    return asyncio.run(client.converse("system prompt", messages, tools or [], max_tokens))


def status_error(status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.invalid/v1/messages")
    reply = httpx2.Response(status, request=request, json={"error": {"message": SENSITIVE}})
    return anthropic.APIStatusError(SENSITIVE, response=reply, body=None)


def test_returns_text_turn_with_usage() -> None:
    fake = FakeAnthropic(response([text("answer")]))

    turn = converse(fake)

    assert turn.text == "answer"
    assert turn.stop_reason == "end_turn"
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (11, 3)


def test_request_maps_system_messages_tools_and_limits() -> None:
    fake = FakeAnthropic(response([text("ok")]))
    messages = [
        Message(role="user", content=[TextBlock(text="investigate")]),
        Message(
            role="assistant",
            content=[ToolUseBlock(id="t1", name="deployment_history", arguments={"a": 1})],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="t1", content={"result": "ok"}),
                ToolResultBlock(tool_use_id="t2", content={"error": "no"}, is_error=True),
            ],
        ),
    ]
    schema = {"type": "object", "properties": {"service": {"type": "string"}}}
    tools = [ToolSpec(name="deployment_history", description="Deploys", input_schema=schema)]

    converse(fake, messages, tools, max_tokens=256)

    request = fake.messages.calls[0]
    assert request["model"] == MODEL
    assert request["system"] == "system prompt"
    assert request["max_tokens"] == 256
    assert "temperature" not in request
    assert request["tools"] == [
        {"name": "deployment_history", "description": "Deploys", "input_schema": schema}
    ]
    assert request["messages"][1]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "deployment_history", "input": {"a": 1}}
    ]
    assert request["messages"][2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": '{"result": "ok"}',
            "is_error": False,
        },
        {
            "type": "tool_result",
            "tool_use_id": "t2",
            "content": '{"error": "no"}',
            "is_error": True,
        },
    ]


def test_tools_are_omitted_when_there_are_none() -> None:
    fake = FakeAnthropic(response([text("ok")]))

    converse(fake)

    assert "tools" not in fake.messages.calls[0]


def test_response_maps_tool_use_and_ignores_other_blocks() -> None:
    thinking = SimpleNamespace(type="thinking", thinking="private reasoning")
    fake = FakeAnthropic(
        response(
            [thinking, text("Checking."), tool_use("t9", "deployment_history", {"x": 1})],
            stop_reason="tool_use",
        )
    )

    turn = converse(fake)

    assert turn.stop_reason == "tool_use"
    assert turn.text == "Checking."
    assert [(call.id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("t9", "deployment_history", {"x": 1})
    ]


@pytest.mark.parametrize(
    ("anthropic_reason", "expected"),
    [
        ("stop_sequence", "end_turn"),
        ("max_tokens", "max_tokens"),
        ("model_context_window_exceeded", "max_tokens"),
        ("refusal", "refused"),
        ("pause_turn", "end_turn"),
    ],
)
def test_stop_reasons_are_normalized(anthropic_reason: str, expected: str) -> None:
    turn = converse(FakeAnthropic(response([text("x")], stop_reason=anthropic_reason)))

    assert turn.stop_reason == expected


@pytest.mark.parametrize(
    ("status", "code", "unavailable"),
    [
        (400, "provider_request_rejected", False),
        (401, "provider_credentials_invalid", True),
        (403, "provider_access_denied", True),
        (404, "provider_request_rejected", False),
        (413, "provider_request_rejected", False),
        (429, "provider_throttled", True),
        (500, "provider_unavailable", True),
        (503, "provider_unavailable", True),
        (529, "provider_unavailable", True),
        (418, "provider_request_failed", False),
    ],
)
def test_http_failures_map_to_safe_codes(status: int, code: str, unavailable: bool) -> None:
    with pytest.raises(ProviderInvocationError) as raised:
        converse(FakeAnthropic(error=status_error(status)))

    assert raised.value.code == code
    assert raised.value.unavailable is unavailable
    assert SENSITIVE not in str(raised.value) and SENSITIVE not in raised.value.message
    assert raised.value.__cause__ is None


def test_connection_and_timeout_failures_are_unavailable() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.invalid/v1/messages")
    errors = [
        anthropic.APIConnectionError(request=request),
        anthropic.APITimeoutError(request=request),
    ]
    for error in errors:
        with pytest.raises(ProviderInvocationError) as raised:
            converse(FakeAnthropic(error=error))

        assert raised.value.code == "provider_unreachable"
        assert raised.value.unavailable is True


@pytest.mark.parametrize(
    "bad", [SimpleNamespace(content=None, stop_reason="end_turn", usage=None), None]
)
def test_unexpected_response_shape_is_rejected(bad) -> None:
    with pytest.raises(ProviderInvocationError) as raised:
        converse(FakeAnthropic(bad))

    assert raised.value.code == "provider_response_invalid"
    assert raised.value.unavailable is False


def test_failures_log_only_codes_never_keys_or_sdk_messages(caplog) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderInvocationError):
        converse(FakeAnthropic(error=status_error(401)))

    assert "code=provider_credentials_invalid http_status=401" in caplog.text
    assert SENSITIVE not in caplog.text and API_KEY not in caplog.text


@pytest.mark.parametrize(("model", "key"), [("", SecretStr(API_KEY)), (MODEL, None)])
def test_missing_model_or_key_is_a_configuration_error(model: str, key) -> None:
    with pytest.raises(ProviderConfigurationError):
        AnthropicModelClient(model, key)


def test_real_client_construction_makes_no_network_call() -> None:
    # The autouse network guard would raise if constructing the client connected anywhere.
    client = AnthropicModelClient(MODEL, SecretStr(API_KEY))

    assert client.model_id == MODEL


# Settings and factory


def anthropic_env(**overrides) -> dict[str, str]:
    base = {"AI_PROVIDER": "anthropic", "AI_MODEL": MODEL, "ANTHROPIC_API_KEY": API_KEY}
    return {**base, **overrides}


def test_anthropic_settings_require_model_and_key_but_not_a_region() -> None:
    settings = Settings.from_environment(anthropic_env())

    assert settings.provider == "anthropic" and settings.aws_region is None
    assert settings.anthropic_api_key.get_secret_value() == API_KEY
    assert API_KEY not in repr(settings) and API_KEY not in str(settings.model_dump())
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"AI_PROVIDER": "anthropic"})
    assert raised.value.errors == ["AI_PROVIDER=anthropic requires AI_MODEL, ANTHROPIC_API_KEY"]


@pytest.mark.parametrize("value", ["short", "x" * 513, "has space " * 4, "é" * 30])
def test_invalid_anthropic_keys_never_echo_the_value(value: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment(anthropic_env(ANTHROPIC_API_KEY=value))

    assert raised.value.errors[0].startswith("ANTHROPIC_API_KEY: ")
    assert value.strip() not in str(raised.value)


def test_factory_builds_each_provider() -> None:
    assert create_model_client(Settings()) is None
    client = create_model_client(Settings.from_environment(anthropic_env()))
    assert isinstance(client, AnthropicModelClient) and client.model_id == MODEL


# End to end: the model orchestrator over the Anthropic adapter


WINDOW = {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"}
PAYMENTS = {"service": "payments-api", "environment": "production", **WINDOW}
ANSWER = (
    '{"summary": "Deployment d-0042 preceded the 5xx alarm.",'
    ' "facts": [{"statement": "Deployment d-0042 started at 10:05 UTC.",'
    ' "evidence_ids": ["deploy:payments-api:d-0042"]}],'
    ' "hypotheses": [{"id": "h-1", "statement": "Deployment d-0042 caused the errors.",'
    ' "supporting_evidence_ids": ["deploy:payments-api:d-0042", "alarm:payments-api:a-pay-001"],'
    ' "contradicting_evidence_ids": [], "confidence": "high", "rationale": "Timing."}],'
    ' "uncertainties": [], "next_steps": [], "remediation_proposals": []}'
)


class ScriptedMessages:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0)


def test_model_orchestrator_runs_end_to_end_over_the_anthropic_adapter() -> None:
    from cloud_incident_service.application import InvestigationService
    from cloud_incident_service.domain import IncidentRequest
    from cloud_incident_service.orchestration import ModelOrchestrator
    from cloud_incident_service.tools import build_default_registry

    fake = FakeAnthropic()
    fake.messages = ScriptedMessages(
        response(
            [
                tool_use("c1", "cloudwatch_signals", PAYMENTS),
                tool_use("c2", "deployment_history", PAYMENTS),
            ],
            stop_reason="tool_use",
        ),
        response([text(ANSWER)]),
    )
    client = AnthropicModelClient(MODEL, SecretStr(API_KEY), client=fake)
    service = InvestigationService(ModelOrchestrator(client), build_default_registry())
    request = IncidentRequest.model_validate(
        {
            "service": "payments-api",
            "environment": "production",
            "symptoms": "Elevated 5xx errors",
            "time_range": WINDOW,
        }
    )

    report = asyncio.run(service.investigate(request))

    assert report.provenance.model == MODEL and report.provenance.orchestrator == "model"
    assert report.hypotheses[0].confidence == "high"
    assert "deploy:payments-api:d-0042" in report.timeline
    first, second = fake.messages.calls
    assert [tool["name"] for tool in first["tools"]] == [
        "cloudwatch_signals",
        "deployment_history",
        "infrastructure_changes",
        "runbook_search",
    ]
    results = second["messages"][-1]["content"]
    assert [block["tool_use_id"] for block in results] == ["c1", "c2"]
    assert "Untrusted tool output" in results[0]["content"]
    assert API_KEY not in str(first) and API_KEY not in str(second)
