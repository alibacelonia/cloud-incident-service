"""The correlation rules every orchestrator's hypotheses must follow (A-3, A-5, A-6, ADR-010).

The deterministic orchestrator satisfies these rules by construction. A model-written answer is
checked against them, and a violation is returned to the model as a repair request listing the
facts it needs (ADR-015).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cloud_incident_service.correlation import (
    CHANGE_KINDS,
    CORRELATION_WINDOW,
    Contradiction,
    correlated_changes,
    find_contradictions,
    signal_onset,
)
from cloud_incident_service.domain import Evidence, Hypothesis, RemediationProposal

_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_WINDOW_MINUTES = int(CORRELATION_WINDOW.total_seconds() // 60)


@dataclass(frozen=True)
class CorrelationContext:
    onset: Evidence | None
    candidates: list[Evidence]
    contradictions: list[Contradiction]

    @classmethod
    def from_events(cls, events: list[Evidence]) -> CorrelationContext:
        onset = signal_onset(events)
        return cls(
            onset=onset,
            candidates=correlated_changes(events, onset) if onset else [],
            contradictions=find_contradictions(events),
        )

    @property
    def candidate_ids(self) -> set[str]:
        return {item.source_id for item in self.candidates}

    def describe(self) -> str:
        """The correlation facts a model needs to correct its answer."""
        if self.onset is None:
            return (
                "There is no abnormal signal (alarm entering ALARM or error log), so "
                "hypotheses and remediation_proposals must be empty lists."
            )
        lead = (
            f"The first abnormal signal is {self.onset.source_id} at "
            f"{self.onset.timestamp:%Y-%m-%d %H:%M} UTC."
        )
        if not self.candidates:
            return (
                f"{lead} No change started within the {_WINDOW_MINUTES} minutes before it, so "
                "hypotheses and remediation_proposals must be empty lists; describe this in "
                "uncertainties and next_steps."
            )
        ids = ", ".join(item.source_id for item in self.candidates)
        return (
            f"{lead} Changes that started within the {_WINDOW_MINUTES} minutes before it, and so "
            f"may be hypotheses: {ids}."
        )


def rule_violations(
    hypotheses: Sequence[Hypothesis],
    remediation: Sequence[RemediationProposal],
    evidence_by_id: dict[str, Evidence],
    context: CorrelationContext,
) -> list[str]:
    problems = []
    candidate_ids = context.candidate_ids
    for hypothesis in hypotheses:
        changes = [
            source_id
            for source_id in hypothesis.supporting_evidence_ids
            if source_id in evidence_by_id and evidence_by_id[source_id].kind in CHANGE_KINDS
        ]
        for source_id in changes:
            if source_id not in candidate_ids:
                problems.append(
                    f"{hypothesis.id} cites {source_id}, which did not start within the "
                    f"{_WINDOW_MINUTES} minutes before the first abnormal signal"
                )
        if not any(source_id in candidate_ids for source_id in changes):
            problems.append(
                f"{hypothesis.id} must cite a correlated change in supporting_evidence_ids"
            )
        if len(candidate_ids) > 1 and hypothesis.confidence == "high":
            problems.append(f"{hypothesis.id} cannot be high: more than one change is correlated")
        for contradiction in context.contradictions:
            for source_id in changes:
                change = evidence_by_id[source_id]
                if change not in contradiction.evidence:
                    continue
                conflicting = [item.source_id for item in contradiction.conflicting_with(change)]
                missing = [
                    sid for sid in conflicting if sid not in hypothesis.contradicting_evidence_ids
                ]
                if missing or hypothesis.confidence != "low":
                    problems.append(
                        f"{hypothesis.id} is contradicted by {', '.join(conflicting)}: list it in "
                        "contradicting_evidence_ids and rate the hypothesis low"
                    )
    confidence = {item.id: item.confidence for item in hypotheses}
    for proposal in remediation:
        if confidence.get(proposal.hypothesis_id) == "low":
            problems.append(
                f"remediation for {proposal.hypothesis_id} is not allowed because it is low "
                "confidence"
            )
    return problems


def order_hypotheses(
    hypotheses: Sequence[Hypothesis], evidence_by_id: dict[str, Evidence]
) -> list[Hypothesis]:
    """ADR-010 ordering: by confidence, then by the cited change closest to the onset."""

    def latest_change(hypothesis: Hypothesis) -> float:
        times = [
            evidence_by_id[source_id].timestamp.timestamp()
            for source_id in hypothesis.supporting_evidence_ids
            if source_id in evidence_by_id and evidence_by_id[source_id].kind in CHANGE_KINDS
        ]
        return max(times, default=0.0)

    return sorted(
        hypotheses,
        key=lambda item: (_CONFIDENCE_ORDER[item.confidence], -latest_change(item)),
    )
