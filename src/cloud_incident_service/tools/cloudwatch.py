from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, StringConstraints

from cloud_incident_service.domain import Evidence
from cloud_incident_service.tools.base import ToolOutput
from cloud_incident_service.tools.fixture_tool import FixtureRecord, FixtureTool

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=300)]
Label = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._/:-]{1,128}$")]
TOOL_NAME = "cloudwatch_signals"


class AlarmStateChange(FixtureRecord):
    timestamp: AwareDatetime
    alarm_name: Label
    previous_state: Literal["OK", "ALARM", "INSUFFICIENT_DATA"]
    new_state: Literal["OK", "ALARM", "INSUFFICIENT_DATA"]
    reason: ShortText

    def to_evidence(self) -> Evidence:
        return Evidence(
            source_id=f"alarm:{self.service}:{self.id}",
            tool=TOOL_NAME,
            service=self.service,
            environment=self.environment,
            timestamp=self.timestamp,
            kind="alarm_state_change",
            summary=(
                f"Alarm {self.alarm_name} changed from {self.previous_state} to "
                f"{self.new_state}: {self.reason}"
            ),
            attributes={
                "alarm_name": self.alarm_name,
                "previous_state": self.previous_state,
                "new_state": self.new_state,
            },
        )


class MetricDatapoint(FixtureRecord):
    timestamp: AwareDatetime
    metric_name: Label
    statistic: Literal["Average", "Sum", "Maximum", "Minimum", "p90", "p99"]
    value: float
    unit: Label

    def to_evidence(self) -> Evidence:
        return Evidence(
            source_id=f"metric:{self.service}:{self.id}",
            tool=TOOL_NAME,
            service=self.service,
            environment=self.environment,
            timestamp=self.timestamp,
            kind="metric_datapoint",
            summary=f"{self.metric_name} {self.statistic} was {self.value:g} {self.unit}",
            attributes={
                "metric_name": self.metric_name,
                "statistic": self.statistic,
                "value": self.value,
                "unit": self.unit,
            },
        )


class LogEvent(FixtureRecord):
    timestamp: AwareDatetime
    level: Literal["ERROR", "WARN", "INFO"]
    message: ShortText

    def to_evidence(self) -> Evidence:
        return Evidence(
            source_id=f"log:{self.service}:{self.id}",
            tool=TOOL_NAME,
            service=self.service,
            environment=self.environment,
            timestamp=self.timestamp,
            kind="log_event",
            summary=f"{self.level} log: {self.message}",
            attributes={"level": self.level},
        )


class CloudWatchSignals(ToolOutput):
    alarms: list[AlarmStateChange]
    metrics: list[MetricDatapoint]
    logs: list[LogEvent]

    def to_evidence(self) -> list[Evidence]:
        return [record.to_evidence() for record in (*self.alarms, *self.metrics, *self.logs)]


class CloudWatchSignalsTool(FixtureTool):
    """Alarm state changes, metric datapoints, and log events within the incident window."""

    name = TOOL_NAME
    output_model = CloudWatchSignals
    collections = {"alarms": "timestamp", "metrics": "timestamp", "logs": "timestamp"}
