from __future__ import annotations

from typing import Protocol

from cloud_incident_service.domain import Findings, IncidentRequest, Provenance
from cloud_incident_service.tools import ToolSession


class Orchestrator(Protocol):
    provenance: Provenance

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
        """Collect evidence through `tools` and return findings for the request."""
