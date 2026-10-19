from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from cloud_incident_service.domain import IncidentRequest
from cloud_incident_service.domain.report import DataSourceStatus, SourceId

HypothesisCategory = Literal["deployment", "infrastructure_change"]
ForbiddenOutcome = Literal["any_hypothesis", "any_remediation", "high_confidence"]
CheckName = Literal["schema", "citations", "hypothesis", "sources", "safety"]


class Expectation(BaseModel):
    """What a correct investigation of the scenario must and must not contain (FR-8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Expected category of the leading hypothesis, or a list when several are equally correct;
    # None means no hypothesis is expected.
    leading_hypothesis: HypothesisCategory | list[HypothesisCategory] | None
    # The change evidence the leading hypothesis must be about (any one of a list).
    leading_change: SourceId | list[SourceId] | None = None
    must_cite: list[SourceId] = Field(default_factory=list)
    # Sources that must not support a hypothesis or be recommended in a next step.
    must_not_cite: list[SourceId] = Field(default_factory=list)
    report_status: Literal["complete", "partial"] | None = None
    data_sources: dict[str, DataSourceStatus] = Field(default_factory=dict)
    forbidden: list[ForbiddenOutcome] = Field(default_factory=list)

    @property
    def allowed_categories(self) -> list[str] | None:
        value = self.leading_hypothesis
        return None if value is None else [value] if isinstance(value, str) else list(value)

    @property
    def allowed_changes(self) -> list[str]:
        value = self.leading_change
        return [] if value is None else [value] if isinstance(value, str) else list(value)


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    description: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    request: IncidentRequest
    expect: Expectation


class ScenarioSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenarios: list[Scenario] = Field(min_length=1)


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    detail: str | None = None


class ScenarioResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    passed: bool
    checks: dict[CheckName, CheckResult]
    report_status: str | None = None
    leading_hypothesis: str | None = None


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    orchestrator: str
    model: str
    results: list[ScenarioResult]

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / len(self.results) if self.results else 0.0
