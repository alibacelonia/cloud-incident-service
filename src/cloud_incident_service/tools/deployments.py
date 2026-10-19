from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, StringConstraints, model_validator

from cloud_incident_service.domain import Evidence
from cloud_incident_service.tools.base import ToolOutput
from cloud_incident_service.tools.fixture_tool import FixtureRecord, FixtureTool

Version = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._+-]{1,64}$")]
TOOL_NAME = "deployment_history"
# A-3: changes up to 60 minutes before the window can explain signals inside it.
CHANGE_LOOKBACK = timedelta(minutes=60)


class Deployment(FixtureRecord):
    version: Version
    previous_version: Version | None = None
    status: Literal["succeeded", "failed", "rolled_back", "in_progress"]
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check_times(self) -> Deployment:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be earlier than started_at")
        return self

    def to_evidence(self) -> Evidence:
        origin = f" from {self.previous_version}" if self.previous_version else ""
        return Evidence(
            source_id=f"deploy:{self.service}:{self.id}",
            tool=TOOL_NAME,
            service=self.service,
            environment=self.environment,
            timestamp=self.started_at,
            kind="deployment",
            summary=f"Deployment {self.id} of version {self.version}{origin}: {self.status}",
            attributes={
                "version": self.version,
                "previous_version": self.previous_version,
                "status": self.status,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            },
        )


class DeploymentHistory(ToolOutput):
    deployments: list[Deployment]

    def to_evidence(self) -> list[Evidence]:
        return [record.to_evidence() for record in self.deployments]


class DeploymentHistoryTool(FixtureTool):
    """Deployments that started within the window or the preceding look-back period."""

    name = TOOL_NAME
    output_model = DeploymentHistory
    collections = {"deployments": "started_at"}
    lookback = CHANGE_LOOKBACK
