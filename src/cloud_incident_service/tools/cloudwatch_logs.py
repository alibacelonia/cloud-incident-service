"""Read-only CloudWatch Logs reader behind the `cloudwatch_signals` tool contract (ADR-016).

Scope is deliberately small: log events only, one account and region, an operator-supplied
allowlist of log groups, and `logs:FilterLogEvents` as the only API call. Model-supplied
arguments cannot name a log group, account, or region. Collection is bounded (pages, events,
bytes, and a deadline), every event is sanitized before it becomes evidence, and a collection that
stops at a limit is reported as `truncated`, never as complete.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, ClassVar, Literal

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
from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

from cloud_incident_service.config import ConfigurationError
from cloud_incident_service.domain import Evidence
from cloud_incident_service.domain.report import SourceId
from cloud_incident_service.domain.request import Environment, ServiceName
from cloud_incident_service.redaction import redact, redact_text
from cloud_incident_service.tools.base import IncidentWindowQuery, ToolOutput, ToolUnavailableError

logger = logging.getLogger(__name__)

TOOL_NAME = "cloudwatch_signals"
# V2-04 limits, approved 2026-10-20.
MAX_PAGES = 3
MAX_EVENTS = 100
PAGE_SIZE = 100
MAX_RAW_BYTES = 64 * 1024
DEADLINE_SECONDS = 4.0
# Per-event text kept after sanitizing; Evidence summaries are limited to 500 characters.
MAX_MESSAGE_CHARS = 300
# Socket bounds that fit inside the deadline: 1 s connect + 2 s read, one attempt in total
# (botocore's `max_attempts` would still allow a retry; `total_max_attempts` does not).
CONNECT_TIMEOUT_SECONDS = 1
READ_TIMEOUT_SECONDS = 2

_ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL", "SEVERE"}
_WARN_LEVELS = {"WARN", "WARNING"}
_INFO_LEVELS = {"INFO", "DEBUG", "TRACE", "NOTICE"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


class LogSourceError(ToolUnavailableError):
    """A log read failed. The class name is the safe, value-free reason shown in traces."""


class LogGroupNotConfiguredError(LogSourceError):
    """No allowlisted log group exists for the service and environment."""


class LogAccessDeniedError(LogSourceError):
    pass


class LogThrottledError(LogSourceError):
    pass


class LogGroupNotFoundError(LogSourceError):
    pass


class LogCredentialsError(LogSourceError):
    pass


class LogUnreachableError(LogSourceError):
    pass


class LogTimeoutError(LogSourceError):
    pass


class LogRequestFailedError(LogSourceError):
    pass


_CLIENT_ERRORS: dict[str, type[LogSourceError]] = {
    "AccessDeniedException": LogAccessDeniedError,
    "AccessDenied": LogAccessDeniedError,
    "ThrottlingException": LogThrottledError,
    "Throttling": LogThrottledError,
    "TooManyRequestsException": LogThrottledError,
    "ResourceNotFoundException": LogGroupNotFoundError,
    "UnrecognizedClientException": LogCredentialsError,
    "ExpiredTokenException": LogCredentialsError,
    "InvalidSignatureException": LogCredentialsError,
    "ServiceUnavailableException": LogUnreachableError,
    "InternalFailure": LogUnreachableError,
}
_CREDENTIAL_ERRORS = (
    NoCredentialsError,
    PartialCredentialsError,
    CredentialRetrievalError,
    TokenRetrievalError,
)


class LiveLogEvent(BaseModel):
    """One sanitized log event, ready to become evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: SourceId
    timestamp: AwareDatetime
    # "log_event" carries a recognized severity; "log_context" is unstructured or unrecognized.
    kind: Literal["log_event", "log_context"]
    level: Literal["ERROR", "WARN", "INFO"] | None = None
    message: Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS + 1)]


class LiveLogSignals(ToolOutput):
    origin: ClassVar[Literal["fixture", "live"]] = "live"

    service: ServiceName
    environment: Environment
    events: list[LiveLogEvent]
    truncated: bool = False

    @property
    def incomplete(self) -> bool:
        return self.truncated

    def to_evidence(self) -> list[Evidence]:
        return [
            Evidence(
                source_id=event.source_id,
                tool=TOOL_NAME,
                service=self.service,
                environment=self.environment,
                timestamp=event.timestamp,
                kind=event.kind,
                summary=(
                    f"{event.level} log: {event.message}"
                    if event.kind == "log_event"
                    else f"Log context: {event.message}"
                ),
                attributes={"level": event.level} if event.kind == "log_event" else {},
            )
            for event in self.events
        ]


class CloudWatchLogsTool:
    """Collects log events for one service and environment from an allowlisted log group."""

    name = TOOL_NAME
    description = (
        "Live log events (not metrics or alarms) for a service and environment within the "
        "incident window, read from CloudWatch Logs. Events with a structured error level are "
        "abnormal signals; other events are context. Collection is capped and may be truncated."
    )
    read_only: Literal[True] = True
    input_model = IncidentWindowQuery
    output_model = LiveLogSignals

    def __init__(
        self,
        log_groups: Mapping[str, str],
        client: Any,
        *,
        deadline_seconds: float = DEADLINE_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._log_groups = dict(log_groups)
        self._client = client
        self._deadline_seconds = deadline_seconds
        self._clock = clock

    async def run(self, query: IncidentWindowQuery) -> dict[str, Any]:
        # V2-02: resolve the allowlisted group before any SDK access.
        log_group = self._log_groups.get(f"{query.service}/{query.environment}")
        if log_group is None:
            raise LogGroupNotConfiguredError(f"{TOOL_NAME} has no log group for this service")

        events, truncated = await self._collect(log_group, query)
        return {
            "data_as_of": self._clock(),
            "service": query.service,
            "environment": query.environment,
            "events": events,
            "truncated": truncated,
        }

    async def _collect(
        self, log_group: str, query: IncidentWindowQuery
    ) -> tuple[list[dict[str, Any]], bool]:
        deadline = time.monotonic() + self._deadline_seconds
        events: list[dict[str, Any]] = []
        raw_bytes = 0
        token: str | None = None
        seen_tokens: set[str] = set()
        truncated = False

        for _ in range(MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not events:
                    raise LogTimeoutError(f"{TOOL_NAME} exceeded its deadline")
                truncated = True
                break
            # V2-03: exactly these arguments, nothing else (no filter pattern, no streams).
            request: dict[str, Any] = {
                "logGroupName": log_group,
                "startTime": _millis(query.start),
                "endTime": _millis(query.end),
                "limit": PAGE_SIZE,
            }
            if token is not None:
                request["nextToken"] = token
            try:
                page = await asyncio.wait_for(
                    asyncio.to_thread(self._client.filter_log_events, **request), remaining
                )
            except TimeoutError:
                if not events:
                    raise LogTimeoutError(f"{TOOL_NAME} exceeded its deadline") from None
                truncated = True
                break
            except Exception as exc:
                raise _classify(exc) from None

            for raw in page.get("events", []):
                size = len(str(raw.get("message", "")).encode())
                if len(events) >= MAX_EVENTS or raw_bytes + size > MAX_RAW_BYTES:
                    truncated = True
                    break
                raw_bytes += size
                normalized = _normalize(raw, log_group, query)
                if normalized is not None:
                    events.append(normalized)
            if truncated:
                break

            token = page.get("nextToken")
            if not token:
                break
            if token in seen_tokens:  # A repeated continuation token would loop forever.
                truncated = True
                break
            seen_tokens.add(token)
        else:
            # The page cap ended the loop; more data remains only if a token is still pending.
            truncated = token is not None

        return events, truncated


def _millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _classify(exc: Exception) -> LogSourceError:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        error_class = _CLIENT_ERRORS.get(code, LogRequestFailedError)
        logger.warning("CloudWatch Logs read failed: aws_error_code=%s", code)
        return error_class(f"{TOOL_NAME} read failed")
    if isinstance(exc, _CREDENTIAL_ERRORS):
        logger.warning("CloudWatch Logs read failed: credentials unavailable")
        return LogCredentialsError(f"{TOOL_NAME} read failed")
    if isinstance(exc, ConnectionError | HTTPClientError):
        logger.warning("CloudWatch Logs read failed: endpoint unreachable")
        return LogUnreachableError(f"{TOOL_NAME} read failed")
    logger.warning("CloudWatch Logs read failed: %s", type(exc).__name__)
    return LogRequestFailedError(f"{TOOL_NAME} read failed")


def _normalize(
    raw: Mapping[str, Any], log_group: str, query: IncidentWindowQuery
) -> dict[str, Any] | None:
    """Turn a raw event into evidence fields, or None to drop it (foreign, outside, or unusable)."""
    timestamp = _timestamp(raw.get("timestamp"))
    message = raw.get("message")
    if (
        timestamp is None
        or not isinstance(message, str)
        or not query.start <= timestamp <= query.end
    ):
        return None

    structured = _parse_json_object(message)
    level: str | None = None
    if structured is not None:
        # V2-05: events about another service or environment are never evidence here.
        if _differs(structured.get("service"), query.service) or _differs(
            structured.get("environment", structured.get("env")), query.environment
        ):
            return None
        level = _level(structured.get("level"))
        text = _summary_text(redact(structured))
    else:
        text = message

    clean = sanitize_message(text)
    if not clean:
        return None
    return {
        "source_id": _source_id(raw, log_group, query.service, timestamp, message),
        "timestamp": timestamp,
        "kind": "log_event" if level else "log_context",
        "level": level,
        "message": clean,
    }


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_json_object(message: str) -> dict[str, Any] | None:
    stripped = message.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _differs(value: object, expected: str) -> bool:
    return isinstance(value, str) and value != expected


def _level(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    upper = value.strip().upper()
    if upper in _ERROR_LEVELS:
        return "ERROR"
    if upper in _WARN_LEVELS:
        return "WARN"
    return "INFO" if upper in _INFO_LEVELS else None


def _summary_text(redacted: dict[str, Any]) -> str:
    for key in ("message", "msg"):
        if isinstance(redacted.get(key), str) and redacted[key].strip():
            return redacted[key]
    return json.dumps(redacted, sort_keys=True, separators=(",", ":"), default=str)


def sanitize_message(text: str) -> str:
    """Remove control characters, URLs, and secret-like values, then shorten (V2-06)."""
    text = _CONTROL.sub(" ", text)
    text = redact_text(_URL.sub("[REDACTED:url]", text))
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS].rstrip() + "…"


def _source_id(
    raw: Mapping[str, Any], log_group: str, service: str, timestamp: datetime, message: str
) -> str:
    """A stable ID derived from event identity; the log group and event ID are never exposed."""
    event_id = raw.get("eventId")
    identity = str(event_id) if event_id else f"{timestamp.isoformat()}\0{message}"
    digest = hashlib.sha256(f"{log_group}\0{identity}".encode()).hexdigest()[:16]
    return f"log:{service}:cw-{digest}"


def create_logs_client(region: str | None) -> Any:
    """A bounded CloudWatch Logs client. Constructed only when LOG_SOURCE=cloudwatch (V2-01)."""
    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
    try:
        return boto3.session.Session(region_name=region).client("logs", config=config)
    except BotoCoreError:
        raise ConfigurationError(
            ["AWS logs client could not be configured. Check AWS_PROFILE and AWS config files."]
        ) from None
