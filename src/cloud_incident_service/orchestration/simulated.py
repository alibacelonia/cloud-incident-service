"""An offline stand-in for a model, used when ORCHESTRATOR=model and AI_PROVIDER=mock.

It exercises the full model-driven path (tool use, untrusted tool payloads, JSON answers,
validation) without a network: it requests every tool once, then answers with the findings the
deterministic rules derive from the tool results it was sent. It demonstrates plumbing, not model
quality.
"""

from __future__ import annotations

from cloud_incident_service.domain import ToolResult
from cloud_incident_service.orchestration.deterministic import tool_arguments
from cloud_incident_service.orchestration.findings import build_findings, summarize_sources
from cloud_incident_service.orchestration.model import ModelFindings, parse_request
from cloud_incident_service.providers import (
    Message,
    ModelTurn,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)


class RuleBasedModelClient:
    model_id = "rule-based-mock"

    async def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_output_tokens: int,
    ) -> ModelTurn:
        request = parse_request(messages[0])
        payloads = [
            block.content["result"]
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock) and not block.is_error
        ]
        if not payloads:
            return ModelTurn(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        id=f"call-{index}",
                        name=spec.name,
                        arguments=tool_arguments(spec.name, request),
                    )
                    for index, spec in enumerate(tools, start=1)
                ],
            )

        results = [ToolResult.model_validate(payload) for payload in payloads]
        findings = build_findings(request, results)
        sources = summarize_sources(request, results)
        source_written = [*sources.source_uncertainties, *sources.instruction_uncertainties]
        answer = ModelFindings(
            summary=findings.summary,
            facts=findings.facts,
            hypotheses=findings.hypotheses,
            uncertainties=[item for item in findings.uncertainties if item not in source_written],
            next_steps=findings.next_steps,
            remediation_proposals=findings.remediation_proposals,
        )
        return ModelTurn(
            stop_reason="end_turn", content=[TextBlock(text=answer.model_dump_json(indent=2))]
        )
