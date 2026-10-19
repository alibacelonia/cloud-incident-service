from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_TIME_WINDOW = timedelta(hours=24)

Environment = Literal["production", "staging", "development"]
ServiceName = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
]


def to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class TimeRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: AwareDatetime
    end: AwareDatetime

    @field_validator("start", "end", mode="before")
    @classmethod
    def _require_timestamp_text(cls, value: object) -> object:
        # Pydantic would otherwise accept numbers as Unix timestamps; FR-1 requires ISO-8601.
        if not isinstance(value, str | datetime):
            raise ValueError("must be an ISO-8601 timestamp with a UTC offset")
        return value

    @field_validator("start", "end")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        return to_utc(value)

    @model_validator(mode="after")
    def _check_window(self) -> TimeRange:
        if self.end <= self.start:
            raise ValueError("end must be later than start")
        if self.end - self.start > MAX_TIME_WINDOW:
            raise ValueError("time range must not exceed 24 hours")
        return self


class IncidentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: ServiceName
    environment: Environment
    symptoms: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
    ]
    time_range: TimeRange = Field(description="Approximate incident window, at most 24 hours")
