from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from cloud_incident_service.domain import IncidentReport, IncidentRequest
from cloud_incident_service.orchestration import Orchestrator


def utc_now() -> datetime:
    return datetime.now(UTC)


class InvestigationService:
    """Runs one investigation and assembles the validated incident report."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        clock: Callable[[], datetime] = utc_now,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._orchestrator = orchestrator
        self._clock = clock
        self._new_id = new_id

    async def investigate(self, request: IncidentRequest) -> IncidentReport:
        investigation_id = self._new_id()
        findings = await self._orchestrator.investigate(request)
        # Re-validating through IncidentReport applies the request-scoped checks (FR-4, FR-5).
        return IncidentReport.model_validate(
            {
                **findings.model_dump(),
                "investigation_id": investigation_id,
                "generated_at": self._clock(),
                "request": request.model_dump(),
                "provenance": self._orchestrator.provenance.model_dump(),
            }
        )
