from __future__ import annotations

from typing import Protocol

from cloud_incident_service.domain import Findings, IncidentRequest, Provenance


class Orchestrator(Protocol):
    provenance: Provenance

    async def investigate(self, request: IncidentRequest) -> Findings:
        """Collect evidence for the request and return validated findings."""
