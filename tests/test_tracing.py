import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from cloud_incident_service.application import InvestigationService, UnverifiedEvidenceError
from cloud_incident_service.config import ConfigurationError, Settings
from cloud_incident_service.domain import Evidence, Findings, IncidentRequest, Provenance
from cloud_incident_service.main import app, get_investigation_service
from cloud_incident_service.orchestration import DeterministicOrchestrator
from cloud_incident_service.providers import ProviderInvocationError
from cloud_incident_service.redaction import redact, redact_text
from cloud_incident_service.tools import (
    IncidentWindowQuery,
    ToolInputError,
    ToolNotRegisteredError,
    ToolOutput,
    ToolRegistry,
    ToolSession,
    build_default_registry,
)
from cloud_incident_service.tracing import (
    FileTraceSink,
    InMemoryTraceSink,
    InvestigationTrace,
    check_trace_directory,
)

FIXED_ID = UUID("00000000-0000-4000-8000-000000000007")
FIXED_TIME = datetime(2026, 10, 19, 11, 5, tzinfo=UTC)
CANARY_KEY = "AKIAEXAMPLECANARY000"
CANARY_ACCOUNT = "210987654321"
CANARY_EMAIL = "oncall@corp-canary.io"
CANARY_TOKEN = "Bearer canary.token.value-12345"
CANARY_ENV_SECRET = "env-secret-canary-value"


def payload(service: str = "payments-api", symptoms: str = "Elevated 5xx errors") -> dict:
    return {
        "service": service,
        "environment": "production",
        "symptoms": symptoms,
        "time_range": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"},
    }


def request(service: str = "payments-api", symptoms: str = "Elevated 5xx errors"):
    return IncidentRequest.model_validate(payload(service, symptoms))


def run(service: InvestigationService, req: IncidentRequest | None = None):
    return asyncio.run(service.investigate(req or request()))


def traced_service(orchestrator=None, registry=None, sink=None) -> InvestigationService:
    return InvestigationService(
        orchestrator or DeterministicOrchestrator(),
        registry or build_default_registry(),
        trace_sink=sink,
        clock=lambda: FIXED_TIME,
        new_id=lambda: FIXED_ID,
    )


# Redaction


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"key {CANARY_KEY} leaked", "aws_access_key_id"),
        (f"account {CANARY_ACCOUNT}", "aws_account_id"),
        (f"arn:aws:iam::{CANARY_ACCOUNT}:role/x", "aws_account_id"),
        (f"contact {CANARY_EMAIL}", "email"),
        (f"header {CANARY_TOKEN}", "bearer_token"),
        ("aws_secret_access_key = abcdEFGHijklMNOPqrstUVWX1234", "aws_secret_access_key"),
    ],
)
def test_redaction_patterns(text: str, kind: str) -> None:
    redacted = redact_text(text)

    assert f"[REDACTED:{kind}]" in redacted
    for canary in (CANARY_KEY, CANARY_ACCOUNT, CANARY_EMAIL, "canary.token", "abcdEFGH"):
        assert canary not in redacted


@pytest.mark.parametrize(
    "text",
    [
        "version 1.8.0",
        "port 5432",
        "latency 2400 ms",
        "id 1234567890123",
        "d-0042",
        "00000000-0000-4000-8000-000000000007",
    ],
)
def test_redaction_leaves_ordinary_values(text: str) -> None:
    assert redact_text(text) == text


def test_redaction_walks_nested_structures_and_sensitive_keys() -> None:
    value = {"outer": [{"note": f"by {CANARY_EMAIL}"}], "api_key": "x", "password": "", "n": 3}

    assert redact(value) == {
        "outer": [{"note": "by [REDACTED:email]"}],
        "api_key": "[REDACTED:secret]",
        "password": "",
        "n": 3,
    }


# Trace content


def test_trace_records_request_tool_calls_validations_and_outcome() -> None:
    sink = InMemoryTraceSink()

    report = run(traced_service(sink=sink))

    [trace] = sink.traces
    assert trace.investigation_id == report.investigation_id == FIXED_ID
    assert trace.request["service"] == "payments-api"
    assert (trace.orchestrator, trace.model, trace.prompt_version) == (
        "deterministic",
        "deterministic",
        None,
    )
    assert [step.tool for step in trace.steps] == [
        "cloudwatch_signals",
        "deployment_history",
        "infrastructure_changes",
        "runbook_search",
    ]
    deploy_step = trace.steps[1]
    assert deploy_step.type == "tool_call"
    assert deploy_step.status == "ok"
    assert deploy_step.source_ids == ["deploy:payments-api:d-0042"]
    assert deploy_step.arguments == {
        "service": "payments-api",
        "environment": "production",
        "start": "2026-10-19T10:00:00Z",
        "end": "2026-10-19T11:00:00Z",
    }
    assert all(step.duration_ms >= 0 for step in trace.steps)
    assert [(item.check, item.passed) for item in trace.validations] == [
        ("report_schema", True),
        ("evidence_provenance", True),
    ]
    assert (trace.outcome, trace.report_status, trace.error) == ("completed", "complete", None)
    assert trace.started_at == trace.finished_at == FIXED_TIME
    traced_ids = {sid for step in trace.steps for sid in step.source_ids}
    assert traced_ids == {item.source_id for item in report.evidence}


class ProbingOrchestrator:
    """Attempts calls the registry must refuse, then runs normally."""

    provenance = Provenance(orchestrator="model", model="example.model-v1:0")

    async def investigate(self, req: IncidentRequest, tools: ToolSession) -> Findings:
        with pytest.raises(ToolNotRegisteredError):
            await tools.invoke("delete_stack", {"stack": "payments", "force": True})
        with pytest.raises(ToolInputError):
            await tools.invoke("deployment_history", {"service": "payments-api"})
        return await DeterministicOrchestrator().investigate(req, tools)


def test_refused_tool_calls_are_traced_without_argument_values() -> None:
    sink = InMemoryTraceSink()

    run(traced_service(orchestrator=ProbingOrchestrator(), sink=sink))

    refused = [step for step in sink.traces[0].steps if step.type == "tool_refused"]
    assert [(step.tool, step.reason, step.argument_names) for step in refused] == [
        ("delete_stack", "not_registered", ["stack", "force"]),
        ("deployment_history", "invalid_arguments", ["service"]),
    ]
    assert "payments" not in json.dumps([step.model_dump() for step in refused]).replace(
        "payments-api", ""
    )


class FabricatingOrchestrator:
    provenance = Provenance(orchestrator="model", model="example.model-v1:0")

    async def investigate(self, req: IncidentRequest, tools: ToolSession) -> Findings:
        invented = Evidence(
            source_id="deploy:payments-api:d-9999",
            tool="deployment_history",
            service="payments-api",
            environment="production",
            timestamp="2026-10-19T10:05:00Z",
            kind="deployment",
            summary="Invented deployment",
        )
        return Findings(status="partial", summary="Invented", evidence=[invented])


def test_failed_provenance_check_is_traced_then_raised() -> None:
    sink = InMemoryTraceSink()

    with pytest.raises(UnverifiedEvidenceError):
        run(traced_service(orchestrator=FabricatingOrchestrator(), sink=sink))

    [trace] = sink.traces
    assert trace.outcome == "failed"
    assert trace.report_status is None
    assert [(item.check, item.passed) for item in trace.validations] == [
        ("report_schema", True),
        ("evidence_provenance", False),
    ]
    assert "deploy:payments-api:d-9999" in trace.validations[1].detail


class FailingOrchestrator:
    provenance = Provenance(orchestrator="model", model="example.model-v1:0")

    async def investigate(self, req: IncidentRequest, tools: ToolSession) -> Findings:
        raise ProviderInvocationError("provider_throttled", "Retry later.", unavailable=True)


def test_orchestrator_failure_is_traced_with_safe_error() -> None:
    sink = InMemoryTraceSink()

    with pytest.raises(ProviderInvocationError):
        run(traced_service(orchestrator=FailingOrchestrator(), sink=sink))

    assert sink.traces[0].outcome == "failed"
    assert sink.traces[0].error == "ProviderInvocationError: provider_throttled"


# Secrets never reach traces or reports (AC-9)


class LeakyOutput(ToolOutput):
    lines: list[str]

    def to_evidence(self) -> list[Evidence]:
        return [
            Evidence(
                source_id=f"log:payments-api:leak-{index}",
                tool="leaky_logs",
                service="payments-api",
                environment="production",
                timestamp="2026-10-19T10:30:00Z",
                kind="log_event",
                summary=line,
                attributes={"level": "ERROR", "token": CANARY_TOKEN},
            )
            for index, line in enumerate(self.lines)
        ]


class LeakyTool:
    name = "leaky_logs"
    read_only = True
    input_model = IncidentWindowQuery
    output_model = LeakyOutput

    async def run(self, query: IncidentWindowQuery) -> dict:
        return {
            "data_as_of": "2026-10-19T23:00:00Z",
            "lines": [
                f"Auth failed for key {CANARY_KEY} in account {CANARY_ACCOUNT}",
                f"Escalated to {CANARY_EMAIL} with header {CANARY_TOKEN}",
            ],
        }


CANARIES = (CANARY_KEY, CANARY_ACCOUNT, CANARY_EMAIL, "canary.token", CANARY_ENV_SECRET)


def test_canary_secrets_never_reach_trace_files_or_reports(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", CANARY_ENV_SECRET)
    monkeypatch.setenv("AI_MODEL", CANARY_ENV_SECRET)
    service = traced_service(registry=ToolRegistry([LeakyTool()]), sink=FileTraceSink(tmp_path))

    report = run(service, request(symptoms=f"Errors since token {CANARY_TOKEN} was rotated"))

    written = (tmp_path / f"{FIXED_ID}.json").read_text(encoding="utf-8")
    reported = report.model_dump_json()
    for canary in CANARIES:
        assert canary not in written
        assert canary not in reported
    assert "[REDACTED:bearer_token]" in written  # from the symptoms
    assert report.request.symptoms == "Errors since token [REDACTED:bearer_token] was rotated"
    assert "[REDACTED:aws_access_key_id]" in report.evidence[0].summary
    assert report.evidence[0].attributes["token"] == "[REDACTED:secret]"


def test_redacted_symptoms_stay_within_the_length_limit() -> None:
    symptoms = ("account 123456789012 " * 100)[:2_000]

    report = run(traced_service(), request(symptoms=symptoms))

    assert len(report.request.symptoms) == 2_000
    assert "123456789012" not in report.request.symptoms


# Sinks and configuration


def test_file_sink_writes_one_valid_json_file_atomically(tmp_path: Path) -> None:
    run(traced_service(sink=FileTraceSink(tmp_path)))

    files = sorted(path.name for path in tmp_path.iterdir())
    assert files == [f"{FIXED_ID}.json"]
    trace = InvestigationTrace.model_validate_json((tmp_path / files[0]).read_text())
    assert trace.outcome == "completed"


class BrokenSink:
    def write(self, trace: InvestigationTrace) -> None:
        raise PermissionError("disk is read-only")


def test_trace_export_failure_does_not_fail_the_investigation(caplog) -> None:
    with caplog.at_level(logging.ERROR):
        report = run(traced_service(sink=BrokenSink()))

    assert report.status == "complete"
    assert f"Trace export failed: investigation_id={FIXED_ID} error=PermissionError" in caplog.text
    assert "read-only" not in caplog.text


def test_traces_are_disabled_unless_trace_dir_is_set(monkeypatch, tmp_path: Path) -> None:
    assert get_investigation_service()._trace_sink is None

    monkeypatch.setenv("TRACE_DIR", str(tmp_path))
    sink = get_investigation_service()._trace_sink
    assert isinstance(sink, FileTraceSink)
    assert sink.directory == tmp_path


@pytest.mark.parametrize("value", ["bad\x00path", "x" * 1_025])
def test_invalid_trace_dir_names_the_variable(value: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"TRACE_DIR": value})

    assert raised.value.errors[0].startswith("TRACE_DIR: ")


def test_check_trace_directory_creates_and_validates(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "traces"
    check_trace_directory(target, create=True)
    assert target.is_dir()

    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(ConfigurationError, match="TRACE_DIR"):
        check_trace_directory(a_file, create=True)
    with pytest.raises(ConfigurationError, match="does not exist"):
        check_trace_directory(tmp_path / "missing", create=False)


def test_startup_creates_trace_dir_and_api_writes_traces(tmp_path: Path, monkeypatch) -> None:
    trace_dir = tmp_path / ".traces"
    monkeypatch.setenv("TRACE_DIR", str(trace_dir))

    with TestClient(app) as client:
        assert trace_dir.is_dir()
        response = client.post("/v1/investigations", json=payload())

    investigation_id = response.json()["investigation_id"]
    trace = json.loads((trace_dir / f"{investigation_id}.json").read_text())
    assert trace["outcome"] == "completed"
    assert len(trace["steps"]) == 4


def test_startup_fails_when_trace_dir_is_a_file(tmp_path: Path, monkeypatch) -> None:
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x")
    monkeypatch.setenv("TRACE_DIR", str(a_file))

    with pytest.raises(ConfigurationError, match="TRACE_DIR"), TestClient(app):
        pass


def test_health_reports_missing_trace_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "missing"))

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["errors"] == ["TRACE_DIR: directory does not exist"]


def test_traces_directory_is_git_ignored() -> None:
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"

    assert ".traces/" in gitignore.read_text().splitlines()
