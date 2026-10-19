from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from cloud_incident_service.domain import Evidence
from cloud_incident_service.domain.request import Environment, ServiceName, to_utc


class IncidentWindowQuery(BaseModel):
    """Input shared by the signal and change tools: the incident's service and window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: ServiceName
    environment: Environment
    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        return to_utc(value)


class ToolOutput(BaseModel):
    """Typed tool output. Subclasses define records and how they become evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_as_of: AwareDatetime

    def to_evidence(self) -> list[Evidence]:
        raise NotImplementedError


class Tool(Protocol):
    """A read-only evidence source. `run` returns raw output that the registry validates."""

    name: str
    read_only: Literal[True]
    input_model: type[BaseModel]
    output_model: type[ToolOutput]

    async def run(self, query: Any) -> Any: ...


class ToolRegistrationError(ValueError):
    """Raised when a tool cannot be registered, for example because it is not read-only."""


class ToolNotRegisteredError(LookupError):
    """Raised when an invocation names a tool that is not registered. Nothing is executed."""


class ToolInputError(ValueError):
    """Raised when tool arguments fail validation. Nothing is executed."""


class ToolUnavailableError(RuntimeError):
    """Raised by a tool when its data source cannot be reached."""
