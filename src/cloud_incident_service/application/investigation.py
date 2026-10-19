from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ValidationError

from cloud_incident_service.domain import Evidence, IncidentReport, IncidentRequest
from cloud_incident_service.domain.request import MAX_SYMPTOMS_LENGTH
from cloud_incident_service.orchestration import Orchestrator
from cloud_incident_service.redaction import redact_text
from cloud_incident_service.tools import ToolRegistry
from cloud_incident_service.tracing import TraceRecorder, TraceSink

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


class UnverifiedEvidenceError(ValueError):
    """Raised when a report contains evidence that no tool returned in this investigation."""


class InvestigationService:
    """Runs one investigation, assembles the validated report, and exports its trace."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        registry: ToolRegistry,
        *,
        trace_sink: TraceSink | None = None,
        clock: Callable[[], datetime] = utc_now,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._orchestrator = orchestrator
        self._registry = registry
        self._trace_sink = trace_sink
        self._clock = clock
        self._new_id = new_id

    async def investigate(self, request: IncidentRequest) -> IncidentReport:
        # Symptoms are free text; redact pasted secrets before tools, reports, or traces see them.
        # Markers can be longer than what they replace, so keep the result within the limit.
        symptoms = redact_text(request.symptoms)[:MAX_SYMPTOMS_LENGTH]
        request = request.model_copy(update={"symptoms": symptoms})
        investigation_id = self._new_id()
        recorder = TraceRecorder(
            investigation_id, request, self._orchestrator.provenance, self._clock()
        )
        try:
            report = await self._run(investigation_id, request, recorder)
        except Exception as exc:
            recorder.fail(exc, self._clock())
            self._export(recorder)
            raise
        recorder.complete(report.status, report.generated_at)
        self._export(recorder)
        return report

    async def _run(
        self, investigation_id: UUID, request: IncidentRequest, recorder: TraceRecorder
    ) -> IncidentReport:
        tools = self._registry.session(observer=recorder)
        findings = await self._orchestrator.investigate(request, tools)
        try:
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
        except ValidationError as exc:
            recorder.record_validation("report_schema", exc)
            raise
        recorder.record_validation("report_schema")
        try:
            _require_returned_evidence(report, tools.returned_evidence)
        except UnverifiedEvidenceError as exc:
            recorder.record_validation("evidence_provenance", exc)
            raise
        recorder.record_validation("evidence_provenance")
        return report

    def _export(self, recorder: TraceRecorder) -> None:
        if self._trace_sink is None:
            return
        trace = recorder.export()
        try:
            self._trace_sink.write(trace)
        except OSError as exc:
            # A failed export must not fail the investigation; log the cause, not the content.
            logger.error(
                "Trace export failed: investigation_id=%s error=%s",
                trace.investigation_id,
                type(exc).__name__,
            )


def _require_returned_evidence(report: IncidentReport, returned: Mapping[str, Evidence]) -> None:
    # FR-5 rule 1: evidence must be exactly what a tool returned during this investigation.
    unverified = sorted(
        item.source_id for item in report.evidence if returned.get(item.source_id) != item
    )
    if unverified:
        raise UnverifiedEvidenceError(
            f"evidence not returned by a tool in this investigation: {', '.join(unverified)}"
        )
