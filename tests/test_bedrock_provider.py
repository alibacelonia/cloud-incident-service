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

from cloud_incident_service.providers import ProviderConfigurationError, ProviderInvocationError
from cloud_incident_service.providers.bedrock import BedrockProvider

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


def invoke(client: FakeBedrockClient) -> str:
    return asyncio.run(BedrockProvider(MODEL, REGION, client=client).generate("prompt"))


def test_returns_text_from_converse_response() -> None:
    client = FakeBedrockClient(response={"output": {"message": {"content": [{"text": "answer"}]}}})

    assert invoke(client) == "answer"
    assert client.calls[0]["modelId"] == MODEL


def test_converse_runs_off_the_event_loop_thread() -> None:
    client = FakeBedrockClient(response={"output": {"message": {"content": [{"text": "answer"}]}}})

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
    [{}, {"output": {"message": {"content": []}}}, {"output": None}],
)
def test_unexpected_response_shape_is_rejected(response: dict) -> None:
    with pytest.raises(ProviderInvocationError) as raised:
        invoke(FakeBedrockClient(response=response))

    assert raised.value.code == "provider_response_invalid"
    assert raised.value.unavailable is False


@pytest.mark.parametrize(("model", "region"), [("", REGION), (MODEL, None)])
def test_missing_model_or_region_is_a_configuration_error(model: str, region: str | None) -> None:
    with pytest.raises(ProviderConfigurationError):
        BedrockProvider(model, region, client=FakeBedrockClient())


def test_unknown_aws_profile_is_a_configuration_error(monkeypatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "profile-that-does-not-exist")

    with pytest.raises(ProviderConfigurationError) as raised:
        BedrockProvider(MODEL, REGION)

    assert "AWS_PROFILE" in str(raised.value)
    assert "profile-that-does-not-exist" not in str(raised.value)


def test_real_client_construction_makes_no_network_call() -> None:
    # The autouse network guard would raise if constructing the client connected anywhere.
    BedrockProvider(MODEL, REGION)
