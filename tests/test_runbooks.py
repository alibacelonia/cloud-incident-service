import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cloud_incident_service.application import InvestigationService
from cloud_incident_service.domain import IncidentRequest
from cloud_incident_service.orchestration import DeterministicOrchestrator
from cloud_incident_service.tools import (
    LexicalRunbookRetriever,
    RunbookSearchTool,
    ToolInputError,
    ToolRegistry,
    build_default_registry,
    load_runbooks,
)
from cloud_incident_service.tools.runbooks import RunbookFormatError, parse_runbook

AS_OF = datetime(2026, 10, 19, 23, 59, tzinfo=UTC)
SAMPLE = """---
id: sample-runbook
services: payments-api, checkout-api
updated: 2026-09-01T00:00:00Z
---
# Sample runbook

Intro text before the first section is ignored.

## Symptoms

Error rate   rises
above 2 percent.

## Next Steps (On-Call)

Page the owning team.
"""


def search_args(service="payments-api", query="Elevated 5xx errors", **extra) -> dict:
    return {"service": service, "environment": "production", "query": query, **extra}


def run_tool(tool: RunbookSearchTool, arguments: dict):
    session = ToolRegistry([tool]).session()
    return asyncio.run(session.invoke("runbook_search", arguments))


def test_parse_runbook_splits_sections_with_stable_ids() -> None:
    sections = parse_runbook(SAMPLE)

    assert [section.source_id for section in sections] == [
        "runbook:sample-runbook:symptoms",
        "runbook:sample-runbook:next-steps-on-call",
    ]
    assert sections[0].body == "Error rate rises above 2 percent."
    assert sections[0].services == ("payments-api", "checkout-api")
    assert sections[0].runbook_title == "Sample runbook"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("# No front matter\n## A\nbody", RunbookFormatError),
        ("---\nid: x\n# Title\n## A\nbody", RunbookFormatError),
        ("---\nid: x\nservices: *\n---\n# Title\n## A\nbody", RunbookFormatError),
        (
            "---\nid: x\nservices: *\nupdated: 2026-09-01T00:00:00Z\n---\n# Title\n",
            RunbookFormatError,
        ),
        (
            "---\nid: x\nservices: *\nupdated: 2026-09-01T00:00:00Z\n---\n# T\n## A\nb\n## A\nc",
            RunbookFormatError,
        ),
        (
            "---\nid: Bad_ID\nservices: *\nupdated: 2026-09-01T00:00:00Z\n---\n# T\n## A\nb",
            ValidationError,
        ),
        ("---\nid: x\nservices: *\nupdated: yesterday\n---\n# T\n## A\nb", ValidationError),
        (
            "---\nid: x\nservices: Not A Service\nupdated: 2026-09-01T00:00:00Z\n---\n# T\n## A\nb",
            ValidationError,
        ),
    ],
    ids=[
        "no-front-matter",
        "unclosed-front-matter",
        "missing-updated",
        "no-sections",
        "duplicate-headings",
        "invalid-id",
        "invalid-updated",
        "invalid-service",
    ],
)
def test_invalid_runbooks_are_rejected(text: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        parse_runbook(text)


def test_packaged_runbooks_load_with_unique_ids() -> None:
    retriever = load_runbooks()

    assert retriever.data_as_of == AS_OF
    assert retriever.search("payments-api", "payments", 10)


def test_ranking_is_deterministic_and_scoped_to_service() -> None:
    retriever = load_runbooks()

    payments = retriever.search("payments-api", "Elevated 5xx errors", 3)

    assert [(item.section.source_id, item.score) for item in payments] == [
        ("runbook:payments-5xx:mitigation", 4),
        ("runbook:payments-5xx:symptoms", 4),
        ("runbook:payments-5xx:diagnosis", 3),
    ]
    assert payments == retriever.search("payments-api", "Elevated 5xx errors", 3)
    assert not any("notify" in item.section.source_id for item in payments)


def test_generic_runbooks_apply_to_every_service() -> None:
    results = load_runbooks().search("checkout-api", "Database connection errors on port 5432", 3)

    assert [item.section.source_id for item in results] == [
        "runbook:database-connectivity:symptoms",
        "runbook:database-connectivity:diagnosis",
        "runbook:database-connectivity:mitigation",
    ]


def test_service_specific_runbook_wins_ties() -> None:
    specific = parse_runbook(
        SAMPLE.replace("services: payments-api, checkout-api", "services: payments-api")
    )
    generic = parse_runbook(
        SAMPLE.replace("id: sample-runbook", "id: aaa-generic").replace(
            "services: payments-api, checkout-api", "services: *"
        )
    )
    retriever = LexicalRunbookRetriever(generic + specific, AS_OF)

    results = retriever.search("payments-api", "page owning team", 2)

    assert [item.section.runbook_id for item in results] == ["sample-runbook", "aaa-generic"]
    assert results[0].score == results[1].score


@pytest.mark.parametrize("query", ["zzz unknown words", "the and of"])
def test_queries_without_matching_terms_return_nothing(query: str) -> None:
    assert load_runbooks().search("payments-api", query, 3) == []


def test_duplicate_section_ids_across_runbooks_are_rejected() -> None:
    sections = parse_runbook(SAMPLE)

    with pytest.raises(RunbookFormatError, match="unique"):
        LexicalRunbookRetriever(sections + sections, AS_OF)


def test_tool_returns_ranked_runbook_evidence() -> None:
    result = run_tool(RunbookSearchTool(), search_args())

    assert result.status == "ok"
    assert result.data_as_of == AS_OF
    by_rank = sorted(result.evidence, key=lambda item: item.attributes["rank"])
    assert [item.source_id for item in by_rank] == [
        "runbook:payments-5xx:mitigation",
        "runbook:payments-5xx:symptoms",
        "runbook:payments-5xx:diagnosis",
    ]
    first = by_rank[0]
    assert first.kind == "runbook_section"
    assert first.service == "payments-api"
    assert first.summary.startswith("Payments API elevated 5xx errors / Mitigation: With approval")


def test_tool_respects_limit_and_reports_empty() -> None:
    assert len(run_tool(RunbookSearchTool(), search_args(limit=1)).evidence) == 1
    assert run_tool(RunbookSearchTool(), search_args(query="zzz unknown")).status == "empty"


@pytest.mark.parametrize(
    "arguments", [search_args(limit=0), search_args(limit=11), search_args(query=" ")]
)
def test_tool_rejects_invalid_arguments(arguments: dict) -> None:
    with pytest.raises(ToolInputError):
        run_tool(RunbookSearchTool(), arguments)


def test_long_sections_are_truncated_into_excerpts() -> None:
    long_body = "Restart the worker pool. " * 40
    sections = parse_runbook(SAMPLE.replace("Page the owning team.", long_body))
    tool = RunbookSearchTool(LexicalRunbookRetriever(sections, AS_OF))

    result = run_tool(tool, search_args(query="worker pool"))

    summary = result.evidence[0].summary
    assert summary.endswith("…")
    assert len(summary) < 500


def test_injected_instructions_in_runbooks_are_returned_as_data_only() -> None:
    registry = build_default_registry()
    service = InvestigationService(DeterministicOrchestrator(), registry)
    request = IncidentRequest.model_validate(
        {
            "service": "notify-api",
            "environment": "production",
            "symptoms": "Elevated 5xx errors",
            "time_range": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"},
        }
    )

    report = asyncio.run(service.investigate(request))

    injected = next(item for item in report.evidence if "notify-operator-notes" in item.source_id)
    assert injected.kind == "runbook_section"
    assert "Ignore all previous instructions" in injected.summary
    # Nothing acted on the text: only the four registered tools ran, once each.
    assert [source.tool for source in report.data_sources] == registry.tool_names
    assert report.hypotheses == []
    assert report.remediation_proposals == []
    # The only event is the (also injected) error log; facts restate it as data.
    assert report.timeline == ["log:notify-api:l-ntf-001"]
    assert all(fact.evidence_ids == ["log:notify-api:l-ntf-001"] for fact in report.facts)
