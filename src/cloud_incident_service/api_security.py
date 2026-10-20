"""API-key authentication and request-size limits for the investigation API (SEC-6, SEC-7).

Implemented as a pure ASGI middleware so that an unauthenticated caller is rejected before the
request body is read or parsed, and so that the body-size limit also covers chunked uploads.
Authentication is on only when `SERVICE_API_KEY` is set (ADR-017). Keys are compared in constant
time and never logged, traced, or echoed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cloud_incident_service.config import ConfigurationError, Settings

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"
PROTECTED_PREFIX = "/v1/"
# FR-1 allows 2,000 symptom characters; 32 KiB leaves ample room for JSON and multi-byte text.
MAX_BODY_BYTES = 32 * 1024


class BodyTooLargeError(BaseException):  # noqa: N818 - a control-flow signal, not an error type
    """Raised inside the receive channel when a body exceeds the limit mid-stream.

    It derives from BaseException on purpose: FastAPI turns any `Exception` raised while reading
    the body into a generic 400, which would hide the 413 this middleware must return.
    """


def api_key_matches(provided: str, expected: str) -> bool:
    # Compare fixed-length digests so neither the key's length nor its prefix leaks via timing.
    return hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


class ApiGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PROTECTED_PREFIX):
            await self.app(scope, receive, send)
            return

        try:
            settings = Settings.from_environment()
        except ConfigurationError as exc:
            await _reject(send, 503, "configuration_invalid", str(exc))
            return

        if settings.service_api_key is not None and not _authorized(
            scope, settings.service_api_key.get_secret_value()
        ):
            logger.warning("Rejected request: missing or invalid API key")
            await _reject(send, 401, "unauthorized", "A valid API key is required.")
            return

        declared = _content_length(scope)
        if declared is not None and declared > MAX_BODY_BYTES:
            await _reject(send, 413, "request_too_large", "The request body is too large.")
            return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLargeError:
            if started:
                raise
            await _reject(send, 413, "request_too_large", "The request body is too large.")


def _authorized(scope: Scope, expected: str) -> bool:
    values = [
        value.decode("latin-1")
        for name, value in scope["headers"]
        if name.lower() == API_KEY_HEADER.lower().encode()
    ]
    # Exactly one header: a duplicated key header is ambiguous and rejected.
    return len(values) == 1 and api_key_matches(values[0], expected)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(send: Send, status: int, code: str, detail: str) -> None:
    body: dict[str, Any] = {"detail": detail, "code": code}
    payload = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b'ApiKey realm="cloud-incident-service"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
