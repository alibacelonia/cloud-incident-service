from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

TRACE_VERSION = "1"


class ToolCallStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    tool: str
    arguments: dict[str, Any]
    status: str
    error: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    duration_ms: float = Field(ge=0)


class ToolRefusedStep(BaseModel):
    """A call that was refused before anything ran (FR-2)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_refused"] = "tool_refused"
    tool: Annotated[str, Field(max_length=64)]
    reason: Literal["not_registered", "invalid_arguments"]
    # Only argument names are kept: refused arguments may come from a model and are untrusted.
    argument_names: list[str] = Field(default_factory=list)


TraceStep = Annotated[ToolCallStep | ToolRefusedStep, Field(discriminator="type")]


class ValidationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: Literal["report_schema", "evidence_provenance"]
    passed: bool
    detail: str | None = None


class InvestigationTrace(BaseModel):
    """Secret-free record of one investigation for debugging and evaluation (FR-7)."""

    model_config = ConfigDict(extra="forbid")

    trace_version: Literal["1"] = TRACE_VERSION
    investigation_id: UUID
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    request: dict[str, Any]
    orchestrator: str
    model: str
    prompt_version: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    validations: list[ValidationOutcome] = Field(default_factory=list)
    outcome: Literal["running", "completed", "failed"] = "running"
    report_status: Literal["complete", "partial"] | None = None
    error: str | None = None

    def finish(self, finished_at: datetime) -> None:
        self.finished_at = finished_at
