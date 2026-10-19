import asyncio
from typing import Any

import pytest

from cloud_incident_service.domain import Evidence
from cloud_incident_service.tools import (
    IncidentWindowQuery,
    ToolInputError,
    ToolNotRegisteredError,
    ToolOutput,
    ToolRegistrationError,
    ToolRegistry,
    build_default_registry,
)

ARGUMENTS = {
    "service": "payments-api",
    "environment": "production",
    "start": "2026-10-19T10:00:00Z",
    "end": "2026-10-19T11:00:00Z",
}


class FakeOutput(ToolOutput):
    items: list[str]

    def to_evidence(self) -> list[Evidence]:
        return [
            Evidence(
                source_id=f"fake:payments-api:{item}",
                tool="fake_tool",
                service="payments-api",
                environment="production",
                timestamp="2026-10-19T10:30:00Z",
                kind="fake",
                summary=f"Fake item {item}",
            )
            for item in self.items
        ]


class FakeTool:
    name = "fake_tool"
    read_only = True
    input_model = IncidentWindowQuery
    output_model = FakeOutput

    def __init__(self, behaviour: Any = None) -> None:
        self.behaviour = behaviour
        self.calls = 0

    async def run(self, query: IncidentWindowQuery) -> Any:
        self.calls += 1
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        if self.behaviour == "slow":
            await asyncio.sleep(1)
        if callable(self.behaviour):
            return self.behaviour()
        return self.behaviour


def invoke(tool: FakeTool, name: str = "fake_tool", arguments=ARGUMENTS, timeout: float = 5.0):
    session = ToolRegistry([tool], timeout_seconds=timeout).session()
    result = asyncio.run(session.invoke(name, arguments))
    return result, session


@pytest.mark.parametrize("read_only", [False, None, "yes", 1])
def test_registry_rejects_tools_not_declared_read_only(read_only) -> None:
    tool = FakeTool()
    tool.read_only = read_only

    with pytest.raises(ToolRegistrationError, match="not declared read-only"):
        ToolRegistry([tool])


def test_registry_rejects_tool_without_read_only_attribute() -> None:
    class WriteCapableTool:
        name = "restart_service"

    with pytest.raises(ToolRegistrationError, match="not declared read-only"):
        ToolRegistry([WriteCapableTool()])


def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(ToolRegistrationError, match="already registered"):
        ToolRegistry([FakeTool(), FakeTool()])


def test_unregistered_tool_is_refused_and_nothing_runs() -> None:
    tool = FakeTool({"data_as_of": "2026-10-19T23:00:00Z", "items": []})

    with pytest.raises(ToolNotRegisteredError, match="delete_stack"):
        invoke(tool, name="delete_stack")
    assert tool.calls == 0


def test_invalid_arguments_are_refused_and_nothing_runs() -> None:
    tool = FakeTool({"data_as_of": "2026-10-19T23:00:00Z", "items": []})

    with pytest.raises(ToolInputError):
        invoke(tool, arguments={**ARGUMENTS, "environment": "qa"})
    assert tool.calls == 0


def test_ok_result_is_recorded_with_returned_evidence() -> None:
    result, session = invoke(FakeTool({"data_as_of": "2026-10-19T23:00:00Z", "items": ["b", "a"]}))

    assert result.status == "ok"
    assert [item.source_id for item in result.evidence] == [
        "fake:payments-api:a",
        "fake:payments-api:b",
    ]
    assert session.results == [result]
    assert set(session.returned_evidence) == {"fake:payments-api:a", "fake:payments-api:b"}


def test_no_records_is_empty() -> None:
    result, _ = invoke(FakeTool({"data_as_of": "2026-10-19T23:00:00Z", "items": []}))

    assert result.status == "empty"
    assert result.evidence == []


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"items": ["a"]},
        {"data_as_of": "not-a-date", "items": []},
        {"data_as_of": "2026-10-19T23:00:00Z", "items": [1], "extra": 1},
    ],
)
def test_invalid_output_is_malformed(raw) -> None:
    result, session = invoke(FakeTool(raw))

    assert result.status == "malformed"
    assert result.evidence == []
    assert "output failed validation" in result.error
    assert session.returned_evidence == {}


def test_tool_exception_is_unavailable_without_leaking_message() -> None:
    result, _ = invoke(FakeTool(RuntimeError("secret connection string")))

    assert result.status == "unavailable"
    assert result.error == "RuntimeError"


def test_slow_tool_times_out_as_unavailable() -> None:
    result, _ = invoke(FakeTool("slow"), timeout=0.05)

    assert result.status == "unavailable"
    assert result.error == "timed out"


def test_default_registry_contains_only_the_read_only_fixture_tools() -> None:
    registry = build_default_registry()

    assert registry.tool_names == [
        "cloudwatch_signals",
        "deployment_history",
        "infrastructure_changes",
    ]


def test_fixture_tools_return_identical_results_on_repeat() -> None:
    session = build_default_registry().session()

    async def run_all():
        return [await session.invoke(name, ARGUMENTS) for name in session.tool_names]

    assert asyncio.run(run_all()) == asyncio.run(run_all())
