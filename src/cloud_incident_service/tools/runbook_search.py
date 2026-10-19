from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from cloud_incident_service.domain import Evidence
from cloud_incident_service.domain.request import Environment, ServiceName
from cloud_incident_service.tools.base import ToolOutput
from cloud_incident_service.tools.runbooks import RunbookId, RunbookRetriever, Slug, load_runbooks

TOOL_NAME = "runbook_search"
EXCERPT_LENGTH = 300
DEFAULT_LIMIT = 3


class RunbookSearchQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: ServiceName
    environment: Environment
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=10)


class RunbookMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runbook_id: RunbookId
    runbook_title: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    section: Slug
    heading: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    excerpt: Annotated[str, StringConstraints(min_length=1, max_length=EXCERPT_LENGTH + 1)]
    updated: AwareDatetime
    rank: int = Field(ge=1)
    score: int = Field(ge=1)
    service: ServiceName
    environment: Environment

    def to_evidence(self) -> Evidence:
        return Evidence(
            source_id=f"runbook:{self.runbook_id}:{self.section}",
            tool=TOOL_NAME,
            service=self.service,
            environment=self.environment,
            timestamp=self.updated,
            kind="runbook_section",
            summary=f"{self.runbook_title} / {self.heading}: {self.excerpt}",
            attributes={
                "runbook_id": self.runbook_id,
                "section": self.section,
                "rank": self.rank,
                "score": self.score,
            },
        )


class RunbookSearchResult(ToolOutput):
    matches: list[RunbookMatch]

    def to_evidence(self) -> list[Evidence]:
        return [match.to_evidence() for match in self.matches]


class RunbookSearchTool:
    """Returns the runbook sections that best match the reported symptoms for a service."""

    name = TOOL_NAME
    description = (
        "Runbook sections that best match a free-text query, such as the reported "
        "symptoms, for a service."
    )
    read_only: Literal[True] = True
    input_model = RunbookSearchQuery
    output_model = RunbookSearchResult

    def __init__(self, retriever: RunbookRetriever | None = None) -> None:
        self._retriever = retriever or load_runbooks()

    async def run(self, query: RunbookSearchQuery) -> dict[str, Any]:
        ranked = self._retriever.search(query.service, query.query, query.limit)
        return {
            "data_as_of": self._retriever.data_as_of,
            "matches": [
                {
                    "runbook_id": item.section.runbook_id,
                    "runbook_title": item.section.runbook_title,
                    "section": item.section.slug,
                    "heading": item.section.heading,
                    "excerpt": _excerpt(item.section.body),
                    "updated": item.section.updated,
                    "rank": rank,
                    "score": item.score,
                    "service": query.service,
                    "environment": query.environment,
                }
                for rank, item in enumerate(ranked, start=1)
            ],
        }


def _excerpt(body: str) -> str:
    if len(body) <= EXCERPT_LENGTH:
        return body
    return body[:EXCERPT_LENGTH].rsplit(" ", 1)[0] + "…"
