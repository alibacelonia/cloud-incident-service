from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from cloud_incident_service.domain import Evidence, ToolResult
from cloud_incident_service.redaction import redact_evidence
from cloud_incident_service.tools.base import (
    Tool,
    ToolInputError,
    ToolNotRegisteredError,
    ToolRegistrationError,
)

logger = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_SECONDS = 5.0


class ToolCallObserver(Protocol):
    """Receives every tool call and refusal in a session, for example to build a trace."""

    def record_tool_call(
        self, arguments: Mapping[str, Any], result: ToolResult, duration_ms: float
    ) -> None: ...

    def record_tool_refused(self, tool: str, reason: str, argument_names: list[str]) -> None: ...


class ToolRegistry:
    """Holds read-only tools only (SEC-1). Registration of anything else fails."""

    def __init__(
        self, tools: Iterable[Tool] = (), *, timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._timeout_seconds = timeout_seconds
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            raise ToolRegistrationError("tool must have a non-empty name")
        if getattr(tool, "read_only", False) is not True:
            raise ToolRegistrationError(f"tool {name!r} is not declared read-only")
        if name in self._tools:
            raise ToolRegistrationError(f"tool {name!r} is already registered")
        self._tools[name] = tool

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    def session(self, observer: ToolCallObserver | None = None) -> ToolSession:
        return ToolSession(self._tools, self._timeout_seconds, observer)


class ToolSession:
    """Tool access for one investigation. Records every result and the evidence returned."""

    def __init__(
        self,
        tools: Mapping[str, Tool],
        timeout_seconds: float,
        observer: ToolCallObserver | None = None,
    ) -> None:
        self._tools = dict(tools)
        self._timeout_seconds = timeout_seconds
        self._observer = observer
        self.results: list[ToolResult] = []
        self.returned_evidence: dict[str, Evidence] = {}

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    async def invoke(self, name: str, arguments: Mapping[str, object]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("Refused unregistered tool: %s", name[:64])
            self._refused(name, "not_registered", arguments)
            raise ToolNotRegisteredError(f"tool {name[:64]!r} is not registered")
        try:
            query = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            self._refused(name, "invalid_arguments", arguments)
            raise ToolInputError(
                f"invalid arguments for tool {name!r}: {exc.error_count()} error(s)"
            ) from None

        started = time.perf_counter()
        result = await self._run(tool, query)
        duration_ms = (time.perf_counter() - started) * 1000
        # Tool output is untrusted: redact secrets before it reaches reports, traces, or models.
        result = result.model_copy(
            update={"evidence": [redact_evidence(item) for item in result.evidence]}
        )
        logger.info(
            "Tool %s finished: status=%s evidence=%d", name, result.status, len(result.evidence)
        )
        self.results.append(result)
        for item in result.evidence:
            self.returned_evidence[item.source_id] = item
        if self._observer is not None:
            self._observer.record_tool_call(query.model_dump(mode="json"), result, duration_ms)
        return result

    def _refused(self, name: str, reason: str, arguments: Mapping[str, object]) -> None:
        if self._observer is not None:
            self._observer.record_tool_refused(name, reason, [str(key) for key in arguments])

    async def _run(self, tool: Tool, query: object) -> ToolResult:
        try:
            raw = await asyncio.wait_for(tool.run(query), timeout=self._timeout_seconds)
        except TimeoutError:
            return ToolResult(tool=tool.name, status="unavailable", error="timed out")
        except Exception as exc:  # Any tool failure is contained as an unavailable source.
            return ToolResult(tool=tool.name, status="unavailable", error=type(exc).__name__)

        try:
            output = tool.output_model.model_validate(raw)
            evidence = output.to_evidence()
        except ValidationError as exc:
            return ToolResult(
                tool=tool.name,
                status="malformed",
                error=f"output failed validation: {exc.error_count()} error(s)",
            )
        return ToolResult(
            tool=tool.name,
            status="ok" if evidence else "empty",
            data_as_of=output.data_as_of,
            evidence=sorted(evidence, key=lambda item: (item.timestamp, item.source_id)),
        )
