from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from cloud_incident_service.domain import Evidence, IncidentReport, IncidentRequest
from cloud_incident_service.orchestration import Orchestrator
from cloud_incident_service.tools import ToolRegistry


def utc_now() -> datetime:
    return datetime.now(UTC)


class UnverifiedEvidenceError(ValueError):
    """Raised when a report contains evidence that no tool returned in this investigation."""


class InvestigationService:
    """Runs one investigation and assembles the validated incident report."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        registry: ToolRegistry,
        *,
        clock: Callable[[], datetime] = utc_now,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._orchestrator = orchestrator
        self._registry = registry
        self._clock = clock
        self._new_id = new_id

    async def investigate(self, request: IncidentRequest) -> IncidentReport:
        investigation_id = self._new_id()
        tools = self._registry.session()
        findings = await self._orchestrator.investigate(request, tools)
        # Re-validating through IncidentReport applies the request-scoped checks (FR-4, FR-5).
        report = IncidentReport.model_validate(
            {
                **findings.model_dump(),
                "investigation_id": investigation_id,
                "generated_at": self._clock(),
                "request": request.model_dump(),
                "provenance": self._orchestrator.provenance.model_dump(),
            }
        )
        _require_returned_evidence(report, tools.returned_evidence)
        return report


def _require_returned_evidence(report: IncidentReport, returned: Mapping[str, Evidence]) -> None:
    # FR-5 rule 1: evidence must be exactly what a tool returned during this investigation.
    unverified = sorted(
        item.source_id for item in report.evidence if returned.get(item.source_id) != item
    )
    if unverified:
        raise UnverifiedEvidenceError(
            f"evidence not returned by a tool in this investigation: {', '.join(unverified)}"
        )
