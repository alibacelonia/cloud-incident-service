from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from cloud_incident_service.domain.report import Evidence, ToolName
from cloud_incident_service.domain.request import to_utc

ToolStatus = Literal["ok", "empty", "unavailable", "malformed"]


class ToolResult(BaseModel):
    """The outcome of one tool invocation, normalized to evidence (FR-2, FR-3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: ToolName
    status: ToolStatus
    data_as_of: AwareDatetime | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    # A safe, value-free description of why the call failed; never raw tool output.
    error: Annotated[str, StringConstraints(max_length=200)] | None = None

    @field_validator("data_as_of")
    @classmethod
    def _normalize_to_utc(cls, value: datetime | None) -> datetime | None:
        return to_utc(value) if value is not None else None
