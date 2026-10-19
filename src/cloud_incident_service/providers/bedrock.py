from __future__ import annotations

import asyncio
import logging
from typing import Any

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError,
    CredentialRetrievalError,
    HTTPClientError,
    NoCredentialsError,
    PartialCredentialsError,
    TokenRetrievalError,
)
from pydantic import ValidationError

from cloud_incident_service.providers.base import (
    ProviderConfigurationError,
    ProviderInvocationError,
)
from cloud_incident_service.providers.model_client import (
    Message,
    ModelTurn,
    TextBlock,
    TokenUsage,
    ToolSpec,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)

_CREDENTIAL_ERRORS = (
    NoCredentialsError,
    PartialCredentialsError,
    CredentialRetrievalError,
    TokenRetrievalError,
)

# Bedrock error codes grouped into safe, stable categories: (code, message, unavailable).
_ACCESS_DENIED = (
    "provider_access_denied",
    "Access to the configured model was denied. Check IAM permissions and model access.",
    True,
)
_CREDENTIALS_REJECTED = (
    "provider_credentials_invalid",
    "AWS credentials were rejected. Refresh or replace them.",
    True,
)
_THROTTLED = ("provider_throttled", "The provider is throttling requests. Retry later.", True)
_UNAVAILABLE = (
    "provider_unavailable",
    "The provider is temporarily unavailable. Retry later.",
    True,
)
_REJECTED = (
    "provider_request_rejected",
    "The provider rejected the request. Check AI_MODEL and model access in AWS_REGION.",
    False,
)
_CLIENT_ERROR_CATEGORIES: dict[str, tuple[str, str, bool]] = {
    "AccessDeniedException": _ACCESS_DENIED,
    "UnrecognizedClientException": _CREDENTIALS_REJECTED,
    "ExpiredTokenException": _CREDENTIALS_REJECTED,
    "InvalidSignatureException": _CREDENTIALS_REJECTED,
    "ThrottlingException": _THROTTLED,
    "ServiceQuotaExceededException": _THROTTLED,
    "ServiceUnavailableException": _UNAVAILABLE,
    "InternalServerException": _UNAVAILABLE,
    "ModelNotReadyException": _UNAVAILABLE,
    "ModelTimeoutException": _UNAVAILABLE,
    "ValidationException": _REJECTED,
    "ResourceNotFoundException": _REJECTED,
}


# Network bounds for a single call; the orchestrator also enforces an overall timeout (A-7).
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 50
_STOP_REASONS = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "guardrail_intervened": "refused",
    "content_filtered": "refused",
}


class BedrockModelClient:
    """Amazon Bedrock Converse adapter with tool use. boto3 calls run off the event loop."""

    def __init__(self, model: str, region: str | None, client: Any | None = None) -> None:
        if not model:
            raise ProviderConfigurationError("AI_MODEL is required for AI_PROVIDER=bedrock")
        if not region:
            raise ProviderConfigurationError("AWS_REGION is required for AI_PROVIDER=bedrock")
        if client is None:
            import boto3
            from botocore.config import Config

            config = Config(
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=READ_TIMEOUT_SECONDS,
                retries={"max_attempts": 2, "mode": "standard"},
            )
            try:
                # A fresh session per client avoids boto3's process-wide cached session.
                session = boto3.session.Session(region_name=region)
                client = session.client("bedrock-runtime", config=config)
            except BotoCoreError:
                raise ProviderConfigurationError(
                    "AWS client could not be configured. Check AWS_PROFILE and AWS config files."
                ) from None
        self._client = client
        self.model_id = model

    async def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_output_tokens: int,
    ) -> ModelTurn:
        request: dict[str, Any] = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": [_to_bedrock_message(message) for message in messages],
            "inferenceConfig": {"maxTokens": max_output_tokens, "temperature": 0},
        }
        if tools:
            request["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {"json": tool.input_schema},
                        }
                    }
                    for tool in tools
                ]
            }
        try:
            response = await asyncio.to_thread(self._client.converse, **request)
        except ClientError as exc:
            raise _classify_client_error(exc) from None
        except _CREDENTIAL_ERRORS:
            raise _invocation_error(
                "provider_credentials_unavailable",
                "AWS credentials could not be found or loaded.",
                unavailable=True,
            ) from None
        except (ConnectionError, HTTPClientError):
            raise _invocation_error(
                "provider_unreachable",
                "The provider endpoint could not be reached.",
                unavailable=True,
            ) from None
        except BotoCoreError:
            raise _invocation_error(
                "provider_unavailable",
                "The provider call failed before a response was received.",
                unavailable=True,
            ) from None
        return _from_bedrock_response(response)


def _to_bedrock_message(message: Message) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            content.append({"text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {"toolUse": {"toolUseId": block.id, "name": block.name, "input": block.arguments}}
            )
        else:
            content.append(
                {
                    "toolResult": {
                        "toolUseId": block.tool_use_id,
                        "content": [{"json": block.content}],
                        "status": "error" if block.is_error else "success",
                    }
                }
            )
    return {"role": message.role, "content": content}


def _from_bedrock_response(response: Any) -> ModelTurn:
    try:
        blocks: list[TextBlock | ToolUseBlock] = []
        for item in response["output"]["message"]["content"]:
            if "text" in item:
                blocks.append(TextBlock(text=item["text"]))
            elif "toolUse" in item:
                tool_use = item["toolUse"]
                blocks.append(
                    ToolUseBlock(
                        id=tool_use["toolUseId"],
                        name=tool_use["name"],
                        arguments=tool_use.get("input") or {},
                    )
                )
        usage = response.get("usage", {})
        return ModelTurn(
            stop_reason=_STOP_REASONS.get(response.get("stopReason"), "end_turn"),
            content=blocks,
            usage=TokenUsage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
            ),
        )
    except (KeyError, TypeError, AttributeError, ValidationError):
        raise _invocation_error(
            "provider_response_invalid",
            "The provider returned a response in an unexpected format.",
            unavailable=False,
        ) from None


def _classify_client_error(exc: ClientError) -> ProviderInvocationError:
    error_code = exc.response.get("Error", {}).get("Code", "")
    code, message, unavailable = _CLIENT_ERROR_CATEGORIES.get(
        error_code,
        ("provider_request_failed", "The provider request failed.", False),
    )
    return _invocation_error(code, message, unavailable=unavailable, aws_error_code=error_code)


def _invocation_error(
    code: str, message: str, *, unavailable: bool, aws_error_code: str = ""
) -> ProviderInvocationError:
    # Only stable codes are logged; SDK messages can contain account or resource identifiers.
    logger.warning("Bedrock invocation failed: code=%s aws_error_code=%s", code, aws_error_code)
    return ProviderInvocationError(code, message, unavailable=unavailable)
