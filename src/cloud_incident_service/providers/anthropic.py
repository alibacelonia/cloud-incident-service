"""Direct Anthropic Messages API adapter with tool use (P-03, ADR-018).

Maps the provider-neutral conversation types to the Messages API and translates every SDK
failure into a `ProviderInvocationError` with a stable code. Raw SDK messages are never exposed:
they can contain request identifiers or echo parts of the request.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from pydantic import SecretStr, ValidationError

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

# Bounds for one call; the orchestrator also enforces an overall deadline (A-7).
REQUEST_TIMEOUT_SECONDS = 50.0
CONNECT_TIMEOUT_SECONDS = 5.0
MAX_RETRIES = 2

_STOP_REASONS = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refused",
}

# HTTP status -> (code, safe message, unavailable). Anything else is a rejected request.
_CREDENTIALS = (
    "provider_credentials_invalid",
    "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.",
    True,
)
_ACCESS_DENIED = (
    "provider_access_denied",
    "Access to the configured model was denied. Check the key's permissions and AI_MODEL.",
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
    "The provider rejected the request. Check AI_MODEL.",
    False,
)
_STATUS_CATEGORIES: dict[int, tuple[str, str, bool]] = {
    400: _REJECTED,
    401: _CREDENTIALS,
    403: _ACCESS_DENIED,
    404: _REJECTED,
    413: _REJECTED,
    422: _REJECTED,
    429: _THROTTLED,
    500: _UNAVAILABLE,
    502: _UNAVAILABLE,
    503: _UNAVAILABLE,
    504: _UNAVAILABLE,
    529: _UNAVAILABLE,  # Anthropic "overloaded"
}


class AnthropicModelClient:
    """Anthropic Messages API client. Network calls are async; construction makes none."""

    def __init__(self, model: str, api_key: SecretStr | None, client: Any | None = None) -> None:
        if not model:
            raise ProviderConfigurationError("AI_MODEL is required for AI_PROVIDER=anthropic")
        if client is None:
            if api_key is None:
                raise ProviderConfigurationError(
                    "ANTHROPIC_API_KEY is required for AI_PROVIDER=anthropic"
                )
            client = anthropic.AsyncAnthropic(
                api_key=api_key.get_secret_value(),
                timeout=anthropic.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
                max_retries=MAX_RETRIES,
            )
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
            "model": self.model_id,
            "system": system,
            "messages": [_to_anthropic_message(message) for message in messages],
            "max_tokens": max_output_tokens,
        }
        if tools:
            request["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]
        try:
            response = await self._client.messages.create(**request)
        except anthropic.APIStatusError as exc:
            raise _classify_status(exc.status_code) from None
        except anthropic.APIConnectionError:
            raise _invocation_error(
                "provider_unreachable",
                "The provider endpoint could not be reached.",
                unavailable=True,
            ) from None
        except anthropic.AnthropicError:
            raise _invocation_error(
                "provider_unavailable",
                "The provider call failed before a response was received.",
                unavailable=True,
            ) from None
        return _from_anthropic_response(response)


def _to_anthropic_message(message: Message) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.arguments}
            )
        else:
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": json.dumps(block.content),
                    "is_error": block.is_error,
                }
            )
    return {"role": message.role, "content": content}


def _from_anthropic_response(response: Any) -> ModelTurn:
    try:
        blocks: list[TextBlock | ToolUseBlock] = []
        for item in response.content:
            if item.type == "text":
                blocks.append(TextBlock(text=item.text))
            elif item.type == "tool_use":
                blocks.append(ToolUseBlock(id=item.id, name=item.name, arguments=item.input or {}))
        usage = response.usage
        return ModelTurn(
            stop_reason=_STOP_REASONS.get(response.stop_reason, "end_turn"),
            content=blocks,
            usage=TokenUsage(
                input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0
            ),
        )
    except (AttributeError, TypeError, ValidationError):
        raise _invocation_error(
            "provider_response_invalid",
            "The provider returned a response in an unexpected format.",
            unavailable=False,
        ) from None


def _classify_status(status_code: int) -> ProviderInvocationError:
    if status_code in _STATUS_CATEGORIES:
        code, message, unavailable = _STATUS_CATEGORIES[status_code]
    elif status_code >= 500:
        code, message, unavailable = _UNAVAILABLE
    else:
        code, message, unavailable = (
            "provider_request_failed",
            "The provider request failed.",
            False,
        )
    return _invocation_error(code, message, unavailable=unavailable, http_status=status_code)


def _invocation_error(
    code: str, message: str, *, unavailable: bool, http_status: int | None = None
) -> ProviderInvocationError:
    # Only stable codes and the HTTP status are logged, never SDK messages or request content.
    logger.warning("Anthropic invocation failed: code=%s http_status=%s", code, http_status)
    return ProviderInvocationError(code, message, unavailable=unavailable)
