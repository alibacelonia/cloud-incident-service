from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from cloud_incident_service.domain import IncidentRequest, Provenance, ToolResult
from cloud_incident_service.redaction import redact, redact_text
from cloud_incident_service.tracing.models import (
    InvestigationTrace,
    ModelTurnStep,
    ToolCallStep,
    ToolRefusedStep,
    ValidationOutcome,
)

DETAIL_LIMIT = 300


class TraceRecorder:
    """Builds the trace for one investigation. Implements the tool session's observer hooks."""

    def __init__(
        self,
        investigation_id: UUID,
        request: IncidentRequest,
        provenance: Provenance,
        started_at: datetime,
    ) -> None:
        self.trace = InvestigationTrace(
            investigation_id=investigation_id,
            started_at=started_at,
            request=request.model_dump(mode="json"),
            orchestrator=provenance.orchestrator,
            model=provenance.model,
            prompt_version=provenance.prompt_version,
        )

    def record_tool_call(
        self, arguments: Mapping[str, Any], result: ToolResult, duration_ms: float
    ) -> None:
        self.trace.steps.append(
            ToolCallStep(
                tool=result.tool,
                arguments=dict(arguments),
                status=result.status,
                error=result.error,
                source_ids=[item.source_id for item in result.evidence],
                duration_ms=round(duration_ms, 3),
            )
        )

    def record_tool_refused(self, tool: str, reason: str, argument_names: list[str]) -> None:
        self.trace.steps.append(
            ToolRefusedStep(
                tool=tool[:64],
                reason=reason,
                argument_names=[name[:64] for name in argument_names[:20]],
            )
        )

    def record_model_turn(
        self,
        stop_reason: str,
        tool_calls: list[str],
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
    ) -> None:
        self.trace.steps.append(
            ModelTurnStep(
                stop_reason=stop_reason,
                tool_calls=[name[:64] for name in tool_calls[:20]],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=round(latency_ms, 3),
            )
        )

    def record_validation(self, check: str, error: Exception | None = None) -> None:
        self.trace.validations.append(
            ValidationOutcome(
                check=check,
                passed=error is None,
                detail=describe_error(error) if error is not None else None,
            )
        )

    def complete(self, report_status: str, finished_at: datetime) -> None:
        self.trace.outcome = "completed"
        self.trace.report_status = report_status
        self.trace.finish(finished_at)

    def fail(self, error: Exception, finished_at: datetime) -> None:
        self.trace.outcome = "failed"
        self.trace.error = describe_error(error)
        self.trace.finish(finished_at)

    def export(self) -> InvestigationTrace:
        """A redacted copy, safe to persist."""
        return InvestigationTrace.model_validate(redact(self.trace.model_dump(mode="json")))


def describe_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        locations = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        detail = f"{error.error_count()} validation error(s) at {', '.join(locations) or 'root'}"
    else:
        code = getattr(error, "code", None)
        detail = (
            f"{type(error).__name__}: {code}"
            if isinstance(code, str)
            else (f"{type(error).__name__}: {error}")
        )
    return redact_text(detail)[:DETAIL_LIMIT]
