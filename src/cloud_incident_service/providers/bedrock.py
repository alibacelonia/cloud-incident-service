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

from cloud_incident_service.providers.base import (
    ProviderConfigurationError,
    ProviderInvocationError,
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


class BedrockProvider:
    def __init__(self, model: str, region: str | None, client: Any | None = None) -> None:
        if not model:
            raise ProviderConfigurationError("AI_MODEL is required for AI_PROVIDER=bedrock")
        if not region:
            raise ProviderConfigurationError("AWS_REGION is required for AI_PROVIDER=bedrock")
        if client is None:
            import boto3

            try:
                # A fresh session per provider avoids boto3's process-wide cached session.
                client = boto3.session.Session(region_name=region).client("bedrock-runtime")
            except BotoCoreError:
                raise ProviderConfigurationError(
                    "AWS client could not be configured. Check AWS_PROFILE and AWS config files."
                ) from None
        self._client = client
        self._model = model

    async def generate(self, prompt: str) -> str:
        try:
            response = await asyncio.to_thread(
                self._client.converse,
                modelId=self._model,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
            )
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

        try:
            return response["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
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
