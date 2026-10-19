from __future__ import annotations

from cloud_incident_service.domain import Findings, IncidentRequest, Provenance, Uncertainty


class DeterministicOrchestrator:
    """Rule-based orchestrator. Evidence sources are added in F-04; until then it collects none."""

    provenance = Provenance(orchestrator="deterministic", model="deterministic")

    async def investigate(self, request: IncidentRequest) -> Findings:
        return Findings(
            status="partial",
            summary=(
                f"No evidence was collected for {request.service} in {request.environment}, "
                "so no findings could be made."
            ),
            uncertainties=[
                Uncertainty(description="No evidence sources are registered, so none were queried.")
            ],
        )
