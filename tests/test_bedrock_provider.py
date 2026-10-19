import asyncio
import threading

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
    TokenRetrievalError,
)

from cloud_incident_service.providers import (
    Message,
    ModelTurn,
    ProviderConfigurationError,
    ProviderInvocationError,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from cloud_incident_service.providers.bedrock import BedrockModelClient

MODEL = "example.model-v1:0"
REGION = "us-east-1"
# Raw SDK messages can contain account identifiers; they must never reach callers.
SENSITIVE_SDK_MESSAGE = "User arn:aws:iam::123456789012:user/example is not authorized"


class FakeBedrockClient:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []
        self.thread_id: int | None = None

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        self.thread_id = threading.get_ident()
        if self.error is not None:
            raise self.error
        return self.response


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": SENSITIVE_SDK_MESSAGE}}, "Converse")


TEXT_RESPONSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": "answer"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 11, "outputTokens": 3, "totalTokens": 14},
}


def invoke(client: FakeBedrockClient, messages=None, tools=None) -> ModelTurn:
    model = BedrockModelClient(MODEL, REGION, client=client)
    messages = messages or [Message(role="user", content=[TextBlock(text="prompt")])]
    return asyncio.run(model.converse("system prompt", messages, tools or [], 512))


def test_returns_text_turn_with_usage() -> None:
    client = FakeBedrockClient(response=TEXT_RESPONSE)

    turn = invoke(client)

    assert turn.text == "answer"
    assert turn.stop_reason == "end_turn"
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (11, 3)
    assert client.calls[0]["modelId"] == MODEL


def test_request_maps_system_messages_tools_and_limits() -> None:
    client = FakeBedrockClient(response=TEXT_RESPONSE)
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
    tools = [
        ToolSpec(name="deployment_history", description="Deploys", input_schema={"type": "object"})
    ]

    invoke(client, messages, tools)

    request = client.calls[0]
    assert request["system"] == [{"text": "system prompt"}]
    assert request["inferenceConfig"] == {"maxTokens": 512, "temperature": 0}
    assert request["toolConfig"]["tools"][0]["toolSpec"] == {
        "name": "deployment_history",
        "description": "Deploys",
        "inputSchema": {"json": {"type": "object"}},
    }
    assert request["messages"][1]["content"] == [
        {"toolUse": {"toolUseId": "t1", "name": "deployment_history", "input": {"a": 1}}}
    ]
    assert request["messages"][2]["content"] == [
        {
            "toolResult": {
                "toolUseId": "t1",
                "content": [{"json": {"result": "ok"}}],
                "status": "success",
            }
        },
        {
            "toolResult": {
                "toolUseId": "t2",
                "content": [{"json": {"error": "no"}}],
                "status": "error",
            }
        },
    ]


def test_tool_config_is_omitted_without_tools() -> None:
    client = FakeBedrockClient(response=TEXT_RESPONSE)

    invoke(client)

    assert "toolConfig" not in client.calls[0]


def test_response_maps_tool_use_and_stop_reasons() -> None:
    response = {
        "output": {
            "message": {
                "content": [
                    {"text": "Checking deployments."},
                    {
                        "toolUse": {
                            "toolUseId": "t9",
                            "name": "deployment_history",
                            "input": {"x": 1},
                        }
                    },
                ]
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 5, "outputTokens": 7},
    }

    turn = invoke(FakeBedrockClient(response=response))

    assert turn.stop_reason == "tool_use"
    assert [(call.id, call.name, call.arguments) for call in turn.tool_calls] == [
        ("t9", "deployment_history", {"x": 1})
    ]


@pytest.mark.parametrize(
    ("bedrock_reason", "expected"),
    [
        ("max_tokens", "max_tokens"),
        ("guardrail_intervened", "refused"),
        ("content_filtered", "refused"),
    ],
)
def test_stop_reasons_are_normalized(bedrock_reason: str, expected: str) -> None:
    turn = invoke(FakeBedrockClient(response={**TEXT_RESPONSE, "stopReason": bedrock_reason}))

    assert turn.stop_reason == expected


def test_converse_runs_off_the_event_loop_thread() -> None:
    client = FakeBedrockClient(response=TEXT_RESPONSE)

    invoke(client)

    assert client.thread_id is not None
    assert client.thread_id != threading.get_ident()


@pytest.mark.parametrize(
    ("aws_code", "expected_code", "unavailable"),
    [
        ("AccessDeniedException", "provider_access_denied", True),
        ("UnrecognizedClientException", "provider_credentials_invalid", True),
        ("ExpiredTokenException", "provider_credentials_invalid", True),
        ("ThrottlingException", "provider_throttled", True),
        ("ServiceUnavailableException", "provider_unavailable", True),
        ("ModelTimeoutException", "provider_unavailable", True),
        ("ValidationException", "provider_request_rejected", False),
        ("ResourceNotFoundException", "provider_request_rejected", False),
        ("SomethingNew", "provider_request_failed", False),
    ],
)
def test_client_errors_map_to_safe_codes(
    aws_code: str, expected_code: str, unavailable: bool
) -> None:
    with pytest.raises(ProviderInvocationError) as raised:
        invoke(FakeBedrockClient(error=client_error(aws_code)))

    assert raised.value.code == expected_code
    assert raised.value.unavailable is unavailable
    assert "123456789012" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (NoCredentialsError(), "provider_credentials_unavailable"),
        (
            TokenRetrievalError(provider="sso", error_msg="expired"),
            "provider_credentials_unavailable",
        ),
        (
            EndpointConnectionError(endpoint_url="https://bedrock.example.invalid"),
            "provider_unreachable",
        ),
        (ReadTimeoutError(endpoint_url="https://bedrock.example.invalid"), "provider_unreachable"),
    ],
)
def test_sdk_errors_are_reported_as_unavailable(error: Exception, expected_code: str) -> None:
    with pytest.raises(ProviderInvocationError) as raised:
        invoke(FakeBedrockClient(error=error))

    assert raised.value.code == expected_code
    assert raised.value.unavailable is True
    assert "example.invalid" not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [{}, {"output": None}, {"output": {"message": {"content": [{"toolUse": {"name": "x"}}]}}}],
)
def test_unexpected_response_shape_is_rejected(response: dict) -> None:
    with pytest.raises(ProviderInvocationError) as raised:
        invoke(FakeBedrockClient(response=response))

    assert raised.value.code == "provider_response_invalid"
    assert raised.value.unavailable is False


@pytest.mark.parametrize(("model", "region"), [("", REGION), (MODEL, None)])
def test_missing_model_or_region_is_a_configuration_error(model: str, region: str | None) -> None:
    with pytest.raises(ProviderConfigurationError):
        BedrockModelClient(model, region, client=FakeBedrockClient())


def test_unknown_aws_profile_is_a_configuration_error(monkeypatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "profile-that-does-not-exist")

    with pytest.raises(ProviderConfigurationError) as raised:
        BedrockModelClient(MODEL, REGION)

    assert "AWS_PROFILE" in str(raised.value)
    assert "profile-that-does-not-exist" not in str(raised.value)


def test_real_client_construction_makes_no_network_call() -> None:
    # The autouse network guard would raise if constructing the client connected anywhere.
    BedrockModelClient(MODEL, REGION)
