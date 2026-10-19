"""Rule-based findings for the deterministic orchestrator (FR-3, FR-4, FR-5).

Confidence rules (A-6):
- low: other evidence contradicts the change's recorded state (A-5);
- high: the only correlated change, followed by both an alarm and an error log, with every change
  source answering;
- medium: anything else.
Remediation is proposed only for medium or high hypotheses and always requires human approval.
"""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from cloud_incident_service.correlation import (
    CORRELATION_WINDOW,
    Contradiction,
    abnormal_signals,
    correlated_changes,
    find_contradictions,
    looks_like_instructions,
    signal_onset,
    stale_tools,
)
from cloud_incident_service.domain import (
    DataSource,
    Evidence,
    Fact,
    Findings,
    Hypothesis,
    IncidentRequest,
    NextStep,
    RemediationProposal,
    ToolResult,
    Uncertainty,
)

RUNBOOK_SEARCH = "runbook_search"
RUNBOOK_SECTION = "runbook_section"
CHANGE_TOOLS = ("deployment_history", "infrastructure_changes")
FAILED_STATUSES = ("unavailable", "malformed")
SUMMARY_LIMIT = 1_000
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class SourceSummary:
    """What the tool results say on their own, independent of how findings are produced."""

    evidence: list[Evidence]
    events: list[Evidence]
    status: Literal["complete", "partial"]
    failed: frozenset[str]
    stale: frozenset[str]
    data_sources: list[DataSource]
    source_uncertainties: list[Uncertainty]
    instruction_uncertainties: list[Uncertainty]

    @property
    def timeline(self) -> list[str]:
        # The timeline holds events only; runbook sections are reference material.
        return [item.source_id for item in self.events]


_STATUS_PRIORITY = {"ok": 0, "empty": 1, "malformed": 2, "unavailable": 3}


def merge_results(results: list[ToolResult]) -> list[ToolResult]:
    """One result per tool, in first-call order, for tools called more than once.

    Evidence is de-duplicated by source ID, the best status wins (ok, empty, malformed,
    unavailable), and the earliest `data_as_of` is kept so staleness is not hidden.
    """
    grouped: dict[str, list[ToolResult]] = {}
    for result in results:
        grouped.setdefault(result.tool, []).append(result)
    merged = []
    for tool, group in grouped.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        best = min(group, key=lambda result: _STATUS_PRIORITY[result.status])
        evidence = {item.source_id: item for result in group for item in result.evidence}
        dates = [result.data_as_of for result in group if result.data_as_of is not None]
        merged.append(
            ToolResult(
                tool=tool,
                status=best.status,
                data_as_of=min(dates) if dates else None,
                evidence=list(evidence.values()),
                error=best.error,
            )
        )
    return merged


def summarize_sources(request: IncidentRequest, results: list[ToolResult]) -> SourceSummary:
    results = merge_results(results)
    evidence = sorted(
        (item for result in results for item in result.evidence),
        key=lambda item: (item.timestamp, item.source_id),
    )
    failed = frozenset(result.tool for result in results if result.status in FAILED_STATUSES)
    stale = frozenset(stale_tools(results, request.time_range.end))
    return SourceSummary(
        evidence=evidence,
        events=[item for item in evidence if item.kind != RUNBOOK_SECTION],
        status="partial" if not results or failed else "complete",
        failed=failed,
        stale=stale,
        data_sources=[
            DataSource(tool=result.tool, status="stale" if result.tool in stale else result.status)
            for result in results
        ],
        source_uncertainties=_source_uncertainties(results, stale),
        instruction_uncertainties=_instruction_uncertainties(evidence),
    )


def build_findings(request: IncidentRequest, results: list[ToolResult]) -> Findings:
    sources = summarize_sources(request, results)
    change_sources_missing = any(tool in sources.failed for tool in CHANGE_TOOLS)
    onset = signal_onset(sources.events)
    changes = correlated_changes(sources.events, onset) if onset else []
    contradictions = find_contradictions(sources.events)
    analysis = _Analysis(
        request, sources.events, onset, changes, contradictions, change_sources_missing
    )
    incomplete = sources.status == "partial" and bool(results)
    return Findings(
        status=sources.status,
        summary=_summary(request, sources.evidence, analysis, incomplete, bool(sources.stale)),
        evidence=sources.evidence,
        timeline=sources.timeline,
        facts=analysis.facts(),
        hypotheses=analysis.hypotheses,
        uncertainties=[
            *sources.source_uncertainties,
            *analysis.uncertainties(),
            *sources.instruction_uncertainties,
        ],
        next_steps=[*analysis.next_steps(), *_runbook_step(sources.evidence, onset)],
        remediation_proposals=analysis.remediation,
        data_sources=sources.data_sources,
    )


class _Analysis:
    """Hypotheses and the facts, steps, and proposals derived from them."""

    def __init__(
        self,
        request: IncidentRequest,
        events: list[Evidence],
        onset: Evidence | None,
        changes: list[Evidence],
        contradictions: list[Contradiction],
        change_sources_missing: bool,
    ) -> None:
        self.request = request
        self.events = events
        self.onset = onset
        self.changes = changes
        self.contradictions = contradictions
        self.change_sources_missing = change_sources_missing
        self.signals = abnormal_signals(events)
        self.hypotheses: list[Hypothesis] = []
        self.remediation: list[RemediationProposal] = []
        self._change_steps: list[NextStep] = []
        if onset is not None:
            self._build_hypotheses(onset)

    def _build_hypotheses(self, onset: Evidence) -> None:
        candidates = [self._assess(change, onset) for change in self.changes]
        candidates.sort(
            key=lambda item: (
                _CONFIDENCE_ORDER[item[1]],
                -item[0].timestamp.timestamp(),
                item[0].source_id,
            )
        )
        for number, (change, confidence, supporting, conflicts, rationale) in enumerate(
            candidates, start=1
        ):
            hypothesis_id = f"h-{number}"
            self.hypotheses.append(
                Hypothesis(
                    id=hypothesis_id,
                    statement=_cause_statement(change),
                    supporting_evidence_ids=[item.source_id for item in supporting],
                    contradicting_evidence_ids=[item.source_id for item in conflicts],
                    confidence=confidence,
                    rationale=rationale,
                )
            )
            self._change_steps.append(
                NextStep(
                    action=_verification_action(change),
                    rationale=f"Confirms or rules out {hypothesis_id}.",
                    hypothesis_ids=[hypothesis_id],
                )
            )
            proposal = self._remediation(change, confidence, hypothesis_id)
            if proposal is not None:
                self.remediation.append(proposal)

    def _assess(self, change: Evidence, onset: Evidence):
        conflicts = [
            other
            for contradiction in self.contradictions
            if change in contradiction.evidence
            for other in contradiction.conflicting_with(change)
        ]
        conflict_ids = {item.source_id for item in conflicts}
        followers = [
            signal
            for signal in self.signals
            if signal.timestamp >= change.timestamp and signal.source_id not in conflict_ids
        ]
        signal_kinds = {signal.kind for signal in followers}

        reasons = []
        if conflicts:
            confidence = "low"
            reasons.append("Other evidence contradicts its recorded state.")
        else:
            if len(self.changes) > 1:
                reasons.append("Other changes are also correlated.")
            if signal_kinds != {"alarm_state_change", "log_event"}:
                reasons.append("Only one type of abnormal signal followed it.")
            if self.change_sources_missing:
                reasons.append(
                    "Some change sources were unavailable, so other changes may be missing."
                )
            confidence = "medium" if reasons else "high"
            if not reasons:
                reasons.append(
                    "It is the only correlated change, and both an alarm and error logs "
                    "followed it."
                )

        lead = _duration(onset.timestamp - change.timestamp)
        count = len(followers)
        rationale = (
            f"It started {lead} before the first abnormal signal, within the "
            f"{_window_minutes()}-minute correlation window, and {count} abnormal "
            f"signal{'s' if count != 1 else ''} followed it. " + " ".join(reasons)
        )
        return change, confidence, [change, *followers], conflicts, rationale

    def _remediation(
        self, change: Evidence, confidence: str, hypothesis_id: str
    ) -> RemediationProposal | None:
        if confidence == "low":
            return None
        attributes = change.attributes
        if change.kind == "deployment":
            previous = attributes.get("previous_version")
            if not previous or attributes.get("status") != "succeeded":
                return None
            action = (
                f"Roll back {self.request.service} in {self.request.environment} from version "
                f"{attributes['version']} to {previous}."
            )
        else:
            action = (
                f"Revert the change to {attributes['resource_type']} {attributes['resource_id']} "
                f"({attributes['change_type']} at {_at(change.timestamp)})."
            )
        return RemediationProposal(
            action=action,
            rationale=f"Reverses the change behind {hypothesis_id}; confirm it before acting.",
            hypothesis_id=hypothesis_id,
            risk="medium",
        )

    def facts(self) -> list[Fact]:
        if self.onset is None:
            return []
        onset = self.onset
        facts = [
            Fact(
                statement=(
                    f"The first abnormal signal was at {_at(onset.timestamp)}: {onset.summary}"
                ),
                evidence_ids=[onset.source_id],
            )
        ]
        facts += [
            Fact(
                statement=(
                    f"{change.summary}, starting at {_at(change.timestamp)}, "
                    f"{_duration(onset.timestamp - change.timestamp)} before the first abnormal "
                    "signal."
                ),
                evidence_ids=[change.source_id, onset.source_id],
            )
            for change in self.changes
        ]
        facts += [
            Fact(
                statement=(
                    f"Alarm {signal.attributes['alarm_name']} entered ALARM at "
                    f"{_at(signal.timestamp)}."
                ),
                evidence_ids=[signal.source_id],
            )
            for signal in self.signals
            if signal.kind == "alarm_state_change" and signal != onset
        ]
        errors = [signal for signal in self.signals if signal.kind == "log_event"]
        if len(errors) == 1:
            statement = f"1 error log event was recorded at {_at(errors[0].timestamp)}."
        elif errors:
            statement = (
                f"{len(errors)} error log events were recorded between "
                f"{_at(errors[0].timestamp)} and {_at(errors[-1].timestamp)}."
            )
        if errors:
            facts.append(
                Fact(statement=statement, evidence_ids=[signal.source_id for signal in errors])
            )
        return facts

    def uncertainties(self) -> list[Uncertainty]:
        items = [
            Uncertainty(
                description=f"Sources disagree about {contradiction.entity}: "
                + "; ".join(
                    f"{item.attributes['entity_state']} according to {item.source_id}"
                    for item in contradiction.evidence
                )
                + ".",
                evidence_ids=contradiction.source_ids,
            )
            for contradiction in self.contradictions
        ]
        if self.events and self.onset is None:
            items.append(
                Uncertainty(
                    description=(
                        "No abnormal signals (alarms entering ALARM or error logs) were found "
                        "in the time range."
                    )
                )
            )
        if self.onset is not None and not self.changes:
            missing = (
                ", but some change sources were unavailable" if self.change_sources_missing else ""
            )
            items.append(
                Uncertainty(
                    description=(
                        "No deployment or infrastructure change started within "
                        f"{_duration(CORRELATION_WINDOW)} before the first abnormal "
                        f"signal{missing}."
                    ),
                    evidence_ids=[self.onset.source_id],
                )
            )
        return items

    def next_steps(self) -> list[NextStep]:
        steps = list(self._change_steps)
        steps += [
            NextStep(
                action=f"Confirm the actual state of {contradiction.entity} with the owning team.",
                rationale="Sources disagree about its state.",
                evidence_ids=contradiction.source_ids,
            )
            for contradiction in self.contradictions
        ]
        if self.onset is not None and not self.changes:
            steps.append(
                NextStep(
                    action=(
                        "Check dependencies, traffic volume, and capacity around "
                        f"{_at(self.onset.timestamp)}."
                    ),
                    rationale="No recorded change explains the first abnormal signal.",
                    evidence_ids=[self.onset.source_id],
                )
            )
        return steps


def _source_uncertainties(results: list[ToolResult], stale: Set[str]) -> list[Uncertainty]:
    if not results:
        return [
            Uncertainty(description="No evidence sources are registered, so none were queried.")
        ]
    items = []
    for result in results:
        tool = result.tool
        if result.status == "empty":
            description = (
                "No runbook matched the reported symptoms."
                if tool == RUNBOOK_SEARCH
                else f"No data from {tool} for this service and time range."
            )
            items.append(Uncertainty(description=description, tool=tool))
        elif result.status == "unavailable":
            items.append(
                Uncertainty(
                    description=f"{tool} was unavailable, so its evidence is missing.", tool=tool
                )
            )
        elif result.status == "malformed":
            items.append(
                Uncertainty(
                    description=f"{tool} returned malformed data, which was excluded.", tool=tool
                )
            )
        if tool in stale and result.data_as_of is not None:
            items.append(
                Uncertainty(
                    description=(
                        f"{tool} data is only current to {_at(result.data_as_of)}, before the end "
                        "of the time range."
                    ),
                    tool=tool,
                )
            )
    return items


def _instruction_uncertainties(evidence: list[Evidence]) -> list[Uncertainty]:
    return [
        Uncertainty(
            description=(
                f"{item.source_id} contains text that resembles instructions; it was treated as "
                "data and not followed."
            ),
            tool=item.tool,
            evidence_ids=[item.source_id],
        )
        for item in evidence
        if looks_like_instructions(item)
    ]


def _runbook_step(evidence: list[Evidence], onset: Evidence | None) -> list[NextStep]:
    sections = [
        item
        for item in evidence
        if item.kind == RUNBOOK_SECTION and not looks_like_instructions(item)
    ]
    if onset is None or not sections:
        return []
    top = min(sections, key=lambda item: (item.attributes.get("rank", 0), item.source_id))
    return [
        NextStep(
            action=(
                f"Review runbook section {top.source_id} and confirm its guidance applies before "
                "acting."
            ),
            rationale="It ranked first for the reported symptoms.",
            evidence_ids=[top.source_id],
        )
    ]


def _summary(
    request: IncidentRequest,
    evidence: list[Evidence],
    analysis: _Analysis,
    incomplete: bool,
    has_stale: bool,
) -> str:
    scope = f"{request.service} in {request.environment}"
    if not evidence:
        text = f"No evidence was collected for {scope}, so no findings could be made."
    elif analysis.hypotheses and analysis.onset is not None:
        leading = analysis.hypotheses[0]
        text = (
            f"Abnormal signals for {scope} began at {_at(analysis.onset.timestamp)}. Leading "
            f"hypothesis ({leading.confidence} confidence): {leading.statement}"
        )
    elif analysis.onset is not None:
        text = (
            f"Abnormal signals for {scope} began at {_at(analysis.onset.timestamp)}, but no "
            f"deployment or infrastructure change preceded them within "
            f"{_duration(CORRELATION_WINDOW)}."
        )
    else:
        text = f"No abnormal signals were found for {scope} in the time range."
    if incomplete:
        text += " Some sources failed, so this assessment is incomplete."
    if has_stale:
        text += " Some sources are stale."
    return text if len(text) <= SUMMARY_LIMIT else text[: SUMMARY_LIMIT - 1] + "…"


def _cause_statement(change: Evidence) -> str:
    attributes = change.attributes
    if change.kind == "deployment":
        return (
            f"Deployment {attributes['deployment_id']} of version {attributes['version']} "
            "caused the abnormal signals."
        )
    return (
        f"The change to {attributes['resource_type']} {attributes['resource_id']} "
        f"({attributes['change_type']} at {_at(change.timestamp)}) caused the abnormal signals."
    )


def _verification_action(change: Evidence) -> str:
    attributes = change.attributes
    if change.kind == "deployment":
        return (
            f"Compare error logs and metrics before and after deployment "
            f"{attributes['deployment_id']}, and review the changes in version "
            f"{attributes['version']}."
        )
    return (
        f"Compare the current configuration of {attributes['resource_type']} "
        f"{attributes['resource_id']} with its state before the change."
    )


def _at(timestamp: datetime) -> str:
    return f"{timestamp:%Y-%m-%d %H:%M} UTC"


def _window_minutes() -> int:
    return int(CORRELATION_WINDOW.total_seconds() // 60)


def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "less than a minute"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
