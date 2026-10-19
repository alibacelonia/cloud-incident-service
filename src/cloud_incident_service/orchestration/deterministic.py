from __future__ import annotations

from cloud_incident_service.domain import (
    DataSource,
    Findings,
    IncidentRequest,
    Provenance,
    ToolResult,
    Uncertainty,
)
from cloud_incident_service.tools import ToolSession

_FAILED = ("unavailable", "malformed")


class DeterministicOrchestrator:
    """Rule-based orchestrator: queries every registered tool once, in registration order."""

    provenance = Provenance(orchestrator="deterministic", model="deterministic")

    async def investigate(self, request: IncidentRequest, tools: ToolSession) -> Findings:
        arguments = {
            "service": request.service,
            "environment": request.environment,
            "start": request.time_range.start,
            "end": request.time_range.end,
        }
        results = [await tools.invoke(name, arguments) for name in tools.tool_names]
        evidence = sorted(
            (item for result in results for item in result.evidence),
            key=lambda item: (item.timestamp, item.source_id),
        )
        partial = not results or any(result.status in _FAILED for result in results)
        return Findings(
            status="partial" if partial else "complete",
            summary=_summary(request, results, len(evidence)),
            evidence=evidence,
            timeline=[item.source_id for item in evidence],
            uncertainties=_uncertainties(results),
            data_sources=[DataSource(tool=result.tool, status=result.status) for result in results],
        )


def _summary(request: IncidentRequest, results: list[ToolResult], evidence_count: int) -> str:
    scope = f"{request.service} in {request.environment}"
    if evidence_count == 0:
        return f"No evidence was collected for {scope}, so no findings could be made."
    answered = sum(1 for result in results if result.status not in _FAILED)
    return (
        f"Collected {evidence_count} evidence items for {scope} from {answered} of "
        f"{len(results)} sources. Correlated findings are not produced yet."
    )


def _uncertainties(results: list[ToolResult]) -> list[Uncertainty]:
    if not results:
        return [
            Uncertainty(description="No evidence sources are registered, so none were queried.")
        ]
    messages = {
        "empty": "No data from {tool} for this service and time range.",
        "unavailable": "{tool} was unavailable, so its evidence is missing.",
        "malformed": "{tool} returned malformed data, which was excluded.",
    }
    return [
        Uncertainty(description=messages[result.status].format(tool=result.tool), tool=result.tool)
        for result in results
        if result.status in messages
    ]
