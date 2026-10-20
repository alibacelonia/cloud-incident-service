from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from cloud_incident_service.domain.request import (
    Environment,
    IncidentRequest,
    ServiceName,
    to_utc,
)

SourceId = Annotated[
    str,
    StringConstraints(
        min_length=3, max_length=200, pattern=r"^[a-z][a-z0-9_-]*(:[A-Za-z0-9._-]+)+$"
    ),
]
HypothesisId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Statement = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)]
Level = Literal["low", "medium", "high"]
DataSourceStatus = Literal["ok", "empty", "stale", "truncated", "unavailable", "malformed"]
AttributeValue = str | int | float | bool | None

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Evidence(BaseModel):
    model_config = _FROZEN

    source_id: SourceId
    tool: ToolName
    service: ServiceName
    environment: Environment
    timestamp: AwareDatetime
    kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        return to_utc(value)


class Fact(BaseModel):
    model_config = _FROZEN

    statement: Statement
    evidence_ids: list[SourceId] = Field(min_length=1)


class Hypothesis(BaseModel):
    model_config = _FROZEN

    id: HypothesisId
    statement: Statement
    supporting_evidence_ids: list[SourceId] = Field(min_length=1)
    contradicting_evidence_ids: list[SourceId] = Field(default_factory=list)
    confidence: Level
    rationale: Statement

    @model_validator(mode="after")
    def _check_evidence_sets(self) -> Hypothesis:
        if set(self.supporting_evidence_ids) & set(self.contradicting_evidence_ids):
            raise ValueError("evidence cannot both support and contradict a hypothesis")
        if self.contradicting_evidence_ids and self.confidence == "high":
            raise ValueError("a hypothesis with contradicting evidence cannot be high confidence")
        return self


class Uncertainty(BaseModel):
    model_config = _FROZEN

    description: Statement
    tool: ToolName | None = None
    evidence_ids: list[SourceId] = Field(default_factory=list)


class NextStep(BaseModel):
    model_config = _FROZEN

    action: Statement
    rationale: Statement
    evidence_ids: list[SourceId] = Field(default_factory=list)
    hypothesis_ids: list[HypothesisId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_motivation(self) -> NextStep:
        if not self.evidence_ids and not self.hypothesis_ids:
            raise ValueError("a next step must reference evidence or a hypothesis")
        return self


class RemediationProposal(BaseModel):
    model_config = _FROZEN

    action: Statement
    rationale: Statement
    hypothesis_id: HypothesisId
    risk: Level
    # Proposals are never executed by the service (SEC-3); approval is always required.
    requires_human_approval: Literal[True] = True


class DataSource(BaseModel):
    model_config = _FROZEN

    tool: ToolName
    status: DataSourceStatus
    origin: Literal["fixture", "live"] = "fixture"


class Provenance(BaseModel):
    model_config = _FROZEN

    orchestrator: Literal["deterministic", "model"]
    model: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    prompt_version: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None


class Findings(BaseModel):
    """What an orchestrator concludes; the application adds identity and provenance."""

    model_config = _FROZEN

    status: Literal["complete", "partial"]
    summary: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000)
    ]
    evidence: list[Evidence] = Field(default_factory=list)
    timeline: list[SourceId] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    next_steps: list[NextStep] = Field(default_factory=list)
    remediation_proposals: list[RemediationProposal] = Field(default_factory=list)
    data_sources: list[DataSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> Findings:
        evidence_by_id = {item.source_id: item for item in self.evidence}
        _require_unique("evidence source_id", (item.source_id for item in self.evidence))
        _require_unique("hypothesis id", (item.id for item in self.hypotheses))
        _require_unique("data source tool", (item.tool for item in self.data_sources))
        _require_unique("timeline entry", self.timeline)

        cited = [
            *self.timeline,
            *(ref for fact in self.facts for ref in fact.evidence_ids),
            *(ref for item in self.hypotheses for ref in item.supporting_evidence_ids),
            *(ref for item in self.hypotheses for ref in item.contradicting_evidence_ids),
            *(ref for item in self.uncertainties for ref in item.evidence_ids),
            *(ref for item in self.next_steps for ref in item.evidence_ids),
        ]
        unknown = sorted({ref for ref in cited if ref not in evidence_by_id})
        if unknown:
            raise ValueError(f"references unknown evidence: {', '.join(unknown)}")

        hypothesis_ids = {item.id for item in self.hypotheses}
        referenced = [
            *(ref for item in self.next_steps for ref in item.hypothesis_ids),
            *(item.hypothesis_id for item in self.remediation_proposals),
        ]
        unknown = sorted({ref for ref in referenced if ref not in hypothesis_ids})
        if unknown:
            raise ValueError(f"references unknown hypotheses: {', '.join(unknown)}")

        ordered = sorted(self.timeline, key=lambda ref: (evidence_by_id[ref].timestamp, ref))
        if self.timeline != ordered:
            raise ValueError("timeline must be ordered by timestamp, then source_id")

        if self.status == "complete":
            if not self.data_sources:
                raise ValueError("a complete report requires at least one data source")
            failed = [
                s.tool
                for s in self.data_sources
                if s.status in ("unavailable", "malformed", "truncated")
            ]
            if failed:
                raise ValueError(
                    f"report must be partial; unavailable sources: {', '.join(failed)}"
                )
        return self


class IncidentReport(Findings):
    investigation_id: UUID
    generated_at: AwareDatetime
    request: IncidentRequest
    provenance: Provenance

    @field_validator("generated_at")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        return to_utc(value)

    @model_validator(mode="after")
    def _check_evidence_scope(self) -> IncidentReport:
        # FR-4: evidence about other services or environments never enters a report.
        foreign = sorted(
            item.source_id
            for item in self.evidence
            if (item.service, item.environment) != (self.request.service, self.request.environment)
        )
        if foreign:
            raise ValueError(f"evidence outside the requested service: {', '.join(foreign)}")
        return self


def _require_unique(label: str, values: Iterable[str]) -> None:
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate {label}: {', '.join(duplicates)}")
