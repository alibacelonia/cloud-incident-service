from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, StringConstraints

from cloud_incident_service.domain import Evidence
from cloud_incident_service.tools.base import ToolOutput
from cloud_incident_service.tools.deployments import CHANGE_LOOKBACK
from cloud_incident_service.tools.fixture_tool import FixtureRecord, FixtureTool

TOOL_NAME = "infrastructure_changes"
Label = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._/:-]{1,128}$")]


class InfrastructureChange(FixtureRecord):
    timestamp: AwareDatetime
    resource_type: Label
    resource_id: Label
    change_type: Literal["created", "modified", "deleted"]
    description: Annotated[str, StringConstraints(min_length=1, max_length=300)]

    def to_evidence(self) -> Evidence:
        return Evidence(
            source_id=f"change:{self.service}:{self.id}",
            tool=TOOL_NAME,
            service=self.service,
            environment=self.environment,
            timestamp=self.timestamp,
            kind="infrastructure_change",
            summary=(
                f"{self.resource_type} {self.resource_id} {self.change_type}: {self.description}"
            ),
            attributes={
                "resource_type": self.resource_type,
                "resource_id": self.resource_id,
                "change_type": self.change_type,
            },
        )


class InfrastructureChanges(ToolOutput):
    changes: list[InfrastructureChange]

    def to_evidence(self) -> list[Evidence]:
        return [record.to_evidence() for record in self.changes]


class InfrastructureChangesTool(FixtureTool):
    """Configuration and infrastructure changes within the window or the look-back period."""

    name = TOOL_NAME
    output_model = InfrastructureChanges
    collections = {"changes": "timestamp"}
    lookback = CHANGE_LOOKBACK
