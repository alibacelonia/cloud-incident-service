from __future__ import annotations

from cloud_incident_service.domain import Findings, IncidentRequest, Provenance
from cloud_incident_service.orchestration.findings import RUNBOOK_SEARCH, build_findings
from cloud_incident_service.tools import ToolSession


class DeterministicOrchestrator:
    """Rule-based orchestrator: queries every registered tool once, in registration order."""

    provenance = Provenance(orchestrator="deterministic", model="deterministic")

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
        results = [await tools.invoke(name, _arguments(name, request)) for name in tools.tool_names]
        return build_findings(request, results)


def _arguments(tool_name: str, request: IncidentRequest) -> dict[str, object]:
    scope = {"service": request.service, "environment": request.environment}
    if tool_name == RUNBOOK_SEARCH:
        return {**scope, "query": request.symptoms}
    return {**scope, "start": request.time_range.start, "end": request.time_range.end}
