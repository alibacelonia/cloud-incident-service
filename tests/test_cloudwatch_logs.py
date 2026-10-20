"""CloudWatch Logs adapter, proven offline with a fake client (V2-01 to V2-09, ADR-016)."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    TokenRetrievalError,
)

from cloud_incident_service.application import InvestigationService
from cloud_incident_service.domain import IncidentRequest, ToolResult
from cloud_incident_service.orchestration import DeterministicOrchestrator
from cloud_incident_service.tools import (
    CloudWatchLogsTool,
    ToolInputError,
    ToolRegistry,
    build_default_registry,
)
from cloud_incident_service.tools.cloudwatch_logs import (
    DEADLINE_SECONDS,
    MAX_EVENTS,
    MAX_MESSAGE_CHARS,
    MAX_PAGES,
    MAX_RAW_BYTES,
    PAGE_SIZE,
    sanitize_message,
)

GROUP = "/example/payments-api/prod"
ACCOUNT_ID = "210987654321"
LOG_GROUPS = {"payments-api/production": GROUP}
START = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
END = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)
ARGS = {
    "service": "payments-api",
    "environment": "production",
    "start": START.isoformat(),
    "end": END.isoformat(),
}
NOW = datetime(2026, 10, 19, 11, 30, tzinfo=UTC)
BASE_MS = int(datetime(2026, 10, 19, 10, 9, tzinfo=UTC).timestamp() * 1000)


class FakeLogs:
    """Serves queued pages and fails on any API other than filter_log_events."""

    def __init__(self, *pages: dict, error: Exception | None = None, delay: float = 0.0) -> None:
        self.pages = list(pages)
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.pages.pop(0) if self.pages else {"events": []}

    def __getattr__(self, name):
        raise AssertionError(f"the adapter used an unexpected API: {name}")


def event(index: int = 0, message: str | None = None, **extra) -> dict:
    structured = json.dumps({"level": "ERROR", "message": f"upstream timeout {index}"})
    return {
        "eventId": f"{index:056d}",
        "timestamp": BASE_MS + index * 1000,
        "message": structured if message is None else message,
        **extra,
    }


def page(count: int = 1, token: str | None = None, start: int = 0) -> dict:
    body: dict = {"events": [event(start + offset) for offset in range(count)]}
    if token:
        body["nextToken"] = token
    return body


def tool_for(client: FakeLogs, groups=LOG_GROUPS, **kwargs) -> CloudWatchLogsTool:
    return CloudWatchLogsTool(groups, client, clock=lambda: NOW, **kwargs)


def collect(client: FakeLogs, arguments=ARGS, groups=LOG_GROUPS, **kwargs) -> ToolResult:
    session = ToolRegistry([tool_for(client, groups, **kwargs)]).session()
    return asyncio.run(session.invoke("cloudwatch_signals", arguments))


# V2-03: exact API, arguments, and pagination


def test_reads_with_exactly_the_approved_arguments() -> None:
    client = FakeLogs(page(1, token="t1"), page(1, start=1))

    collect(client)

    assert client.calls == [
        {
            "logGroupName": GROUP,
            "startTime": int(START.timestamp() * 1000),
            "endTime": int(END.timestamp() * 1000),
            "limit": PAGE_SIZE,
        },
        {
            "logGroupName": GROUP,
            "startTime": int(START.timestamp() * 1000),
            "endTime": int(END.timestamp() * 1000),
            "limit": PAGE_SIZE,
            "nextToken": "t1",
        },
    ]


def test_only_filter_log_events_is_ever_used() -> None:
    # FakeLogs raises AssertionError for any other attribute, so a write API or a Logs Insights
    # query would fail here; the tool also exposes no argument that could request one.
    assert collect(FakeLogs(page(1))).status == "ok"
    with pytest.raises(AssertionError):
        FakeLogs().start_query()


# V2-02: allowlist and no model-controlled targets


def test_unmapped_service_fails_before_any_sdk_call() -> None:
    client = FakeLogs(page(1))

    result = collect(client, {**ARGS, "service": "orders-api"})

    assert result.status == "unavailable" and result.error == "LogGroupNotConfiguredError"
    assert client.calls == []


def test_unmapped_environment_fails_before_any_sdk_call() -> None:
    client = FakeLogs(page(1))

    result = collect(client, {**ARGS, "environment": "staging"})

    assert result.status == "unavailable" and client.calls == []


@pytest.mark.parametrize("extra", ["log_group", "logGroupName", "account", "region", "query"])
def test_arguments_cannot_name_a_log_group_account_region_or_query(extra: str) -> None:
    client = FakeLogs(page(1))

    with pytest.raises(ToolInputError):
        collect(client, {**ARGS, extra: "/other/group"})
    assert client.calls == []


def test_each_service_reads_only_its_own_group() -> None:
    groups = {**LOG_GROUPS, "orders-api/production": "/example/orders-api/prod"}
    client = FakeLogs(page(1))

    collect(client, {**ARGS, "service": "orders-api"}, groups)

    assert [call["logGroupName"] for call in client.calls] == ["/example/orders-api/prod"]


# V2-04: bounds and truncation


def test_complete_collection_is_ok_and_not_truncated() -> None:
    result = collect(FakeLogs(page(3)))

    assert result.status == "ok" and result.origin == "live" and len(result.evidence) == 3
    assert result.data_as_of == NOW


def test_empty_response_is_empty_not_failed() -> None:
    client = FakeLogs({"events": []})

    result = collect(client)

    assert result.status == "empty" and result.evidence == [] and result.origin == "live"


def test_empty_pages_with_tokens_are_followed_up_to_the_page_cap() -> None:
    client = FakeLogs(
        {"events": [], "nextToken": "a"},
        {"events": [], "nextToken": "b"},
        page(2, start=0),
    )

    result = collect(client)

    assert len(client.calls) == 3 and result.status == "ok" and len(result.evidence) == 2


def test_page_cap_with_more_data_is_truncated() -> None:
    client = FakeLogs(
        page(5, token="a", start=0), page(5, token="b", start=10), page(5, token="c", start=20)
    )

    result = collect(client)

    assert len(client.calls) == MAX_PAGES == 3
    assert result.status == "truncated" and len(result.evidence) == 15


def test_last_page_without_token_is_not_truncated() -> None:
    client = FakeLogs(page(5, token="a"), page(5, token="b", start=10), page(5, start=20))

    assert collect(client).status == "ok"


def test_event_cap_is_truncated_and_never_exceeded() -> None:
    client = FakeLogs(page(PAGE_SIZE, token="a"), page(50, start=PAGE_SIZE))

    result = collect(client)

    assert result.status == "truncated" and len(result.evidence) == MAX_EVENTS == 100
    assert len(client.calls) == 2


def test_exactly_the_event_cap_without_more_data_is_not_truncated() -> None:
    result = collect(FakeLogs(page(MAX_EVENTS)))

    assert result.status == "ok" and len(result.evidence) == MAX_EVENTS


def test_exact_cap_followed_by_an_empty_page_is_not_truncated() -> None:
    result = collect(FakeLogs(page(MAX_EVENTS, token="a"), {"events": []}))

    assert result.status == "ok"


def test_byte_cap_is_truncated() -> None:
    big = "x" * 4_000
    events = [event(i, message=big) for i in range(MAX_RAW_BYTES // 4_000 + 3)]
    client = FakeLogs({"events": events})

    result = collect(client)

    assert result.status == "truncated"
    assert len(result.evidence) == MAX_RAW_BYTES // 4_000


def test_repeated_continuation_token_stops_collection() -> None:
    client = FakeLogs(page(1, token="same"), page(1, token="same", start=1), page(1, start=2))

    result = collect(client)

    assert result.status == "truncated" and len(client.calls) == 2


def test_deadline_with_no_events_is_unavailable() -> None:
    client = FakeLogs(page(1), delay=0.5)

    result = collect(client, deadline_seconds=0.05)

    assert result.status == "unavailable" and result.error == "LogTimeoutError"


def test_deadline_after_some_events_is_truncated() -> None:
    class SlowSecondPage(FakeLogs):
        def filter_log_events(self, **kwargs):
            if self.calls:
                time.sleep(0.5)
            return super().filter_log_events(**kwargs)

    client = SlowSecondPage(page(2, token="a"), page(2, start=2))

    result = collect(client, deadline_seconds=0.2)

    assert result.status == "truncated" and len(result.evidence) == 2


def test_adapter_deadline_fits_inside_the_tool_timeout() -> None:
    from cloud_incident_service.tools.registry import DEFAULT_TOOL_TIMEOUT_SECONDS

    assert DEADLINE_SECONDS == 4.0 < DEFAULT_TOOL_TIMEOUT_SECONDS == 5.0


def test_client_is_built_with_bounded_sockets_and_no_retries() -> None:
    from cloud_incident_service.tools.cloudwatch_logs import create_logs_client

    client = create_logs_client("us-east-1")

    assert client.meta.config.connect_timeout == 1 and client.meta.config.read_timeout == 2
    assert client.meta.config.retries["total_max_attempts"] == 1


# V2-07: honest failures


@pytest.mark.parametrize(
    ("aws_code", "expected"),
    [
        ("AccessDeniedException", "LogAccessDeniedError"),
        ("ThrottlingException", "LogThrottledError"),
        ("ResourceNotFoundException", "LogGroupNotFoundError"),
        ("ExpiredTokenException", "LogCredentialsError"),
        ("ServiceUnavailableException", "LogUnreachableError"),
        ("SomethingNew", "LogRequestFailedError"),
    ],
)
def test_client_errors_become_unavailable_with_a_safe_reason(aws_code: str, expected: str) -> None:
    sensitive = f"User arn:aws:iam::{ACCOUNT_ID}:user/example cannot read {GROUP}"
    error = ClientError({"Error": {"Code": aws_code, "Message": sensitive}}, "FilterLogEvents")

    result = collect(FakeLogs(error=error))

    assert result.status == "unavailable" and result.error == expected
    assert ACCOUNT_ID not in result.model_dump_json() and GROUP not in result.model_dump_json()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NoCredentialsError(), "LogCredentialsError"),
        (TokenRetrievalError(provider="sso", error_msg="expired"), "LogCredentialsError"),
        (EndpointConnectionError(endpoint_url="https://logs.invalid"), "LogUnreachableError"),
        (RuntimeError(f"boom {ACCOUNT_ID}"), "LogRequestFailedError"),
    ],
)
def test_sdk_failures_become_unavailable(error: Exception, expected: str) -> None:
    result = collect(FakeLogs(error=error))

    assert result.status == "unavailable" and result.error == expected
    assert ACCOUNT_ID not in result.model_dump_json() and "logs.invalid" not in result.error


def test_failures_log_codes_not_messages(caplog) -> None:
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"{GROUP} {ACCOUNT_ID}"}}, "x"
    )

    with caplog.at_level(logging.DEBUG):
        collect(FakeLogs(error=error))

    assert "aws_error_code=AccessDeniedException" in caplog.text
    assert GROUP not in caplog.text and ACCOUNT_ID not in caplog.text


# V2-05: normalization and severity


def test_structured_error_logs_are_abnormal_signals_and_others_are_context() -> None:
    events = [
        event(1, json.dumps({"level": "error", "message": "db down"})),
        event(2, json.dumps({"level": "FATAL", "msg": "crashed"})),
        event(3, json.dumps({"level": "WARNING", "message": "slow"})),
        event(4, json.dumps({"level": "debug", "message": "trace"})),
        event(5, json.dumps({"level": "verbose", "message": "unrecognized level"})),
        event(6, json.dumps({"message": "no level at all"})),
        event(7, "Plain text ERROR: connection refused"),
        event(8, json.dumps(["not", "an", "object"])),
    ]

    result = collect(FakeLogs({"events": events}))

    by_summary = {item.summary: item for item in result.evidence}
    assert by_summary["ERROR log: db down"].kind == "log_event"
    assert by_summary["ERROR log: crashed"].attributes == {"level": "ERROR"}
    assert by_summary["WARN log: slow"].attributes == {"level": "WARN"}
    assert by_summary["INFO log: trace"].attributes == {"level": "INFO"}
    context = [item for item in result.evidence if item.kind == "log_context"]
    assert len(context) == 4 and all(item.attributes == {} for item in context)
    # Free text containing the word ERROR is context only (arbitrary text cannot be a signal).
    assert "Log context: Plain text ERROR: connection refused" in by_summary


def test_events_for_other_services_or_environments_are_dropped() -> None:
    events = [
        event(1, json.dumps({"level": "ERROR", "message": "ours", "service": "payments-api"})),
        event(
            2, json.dumps({"level": "ERROR", "message": "other service", "service": "ledger-api"})
        ),
        event(3, json.dumps({"level": "ERROR", "message": "staging", "environment": "staging"})),
        event(4, json.dumps({"level": "ERROR", "message": "env key", "env": "development"})),
        event(5, json.dumps({"level": "ERROR", "message": "production ok", "env": "production"})),
    ]

    result = collect(FakeLogs({"events": events}))

    assert sorted(item.summary for item in result.evidence) == [
        "ERROR log: ours",
        "ERROR log: production ok",
    ]
    assert {(item.service, item.environment) for item in result.evidence} == {
        ("payments-api", "production")
    }


def test_timestamps_are_utc_and_events_outside_the_window_are_dropped() -> None:
    inside = event(1)
    before = {**event(2), "timestamp": int(START.timestamp() * 1000) - 1}
    after = {**event(3), "timestamp": int(END.timestamp() * 1000) + 1}
    bad = [{**event(4), "timestamp": "yesterday"}, {**event(5), "timestamp": None}]

    result = collect(FakeLogs({"events": [inside, before, after, *bad]}))

    [item] = result.evidence
    assert item.timestamp == datetime.fromtimestamp(inside["timestamp"] / 1000, UTC)
    assert item.timestamp.utcoffset().total_seconds() == 0


def test_events_without_a_message_are_dropped() -> None:
    events = [{"eventId": "1", "timestamp": BASE_MS}, {**event(2), "message": 42}, event(3, "   ")]

    assert collect(FakeLogs({"events": events})).status == "empty"


def test_source_ids_are_stable_and_hide_the_event_id_and_log_group() -> None:
    first = collect(FakeLogs(page(2)))
    second = collect(FakeLogs(page(2)))
    elsewhere = collect(
        FakeLogs(page(2)), groups={"payments-api/production": "/example/other/group"}
    )

    ids = [item.source_id for item in first.evidence]
    assert ids == [item.source_id for item in second.evidence]
    assert ids != [item.source_id for item in elsewhere.evidence]
    assert all(source_id.startswith("log:payments-api:cw-") for source_id in ids)
    assert len(set(ids)) == 2
    assert not any(f"{0:056d}" in source_id or "example" in source_id for source_id in ids)


def test_events_without_an_event_id_still_get_stable_distinct_ids() -> None:
    raw = [{"timestamp": BASE_MS, "message": "a"}, {"timestamp": BASE_MS, "message": "b"}]

    first = collect(FakeLogs({"events": raw}))
    second = collect(FakeLogs({"events": raw}))

    assert [e.source_id for e in first.evidence] == [e.source_id for e in second.evidence]
    assert len({e.source_id for e in first.evidence}) == 2


# V2-06: sanitization


def test_secrets_urls_and_identifiers_never_survive_into_evidence() -> None:
    canaries = {
        "AKIAEXAMPLECANARY000",
        ACCOUNT_ID,
        "oncall@canary-team.example",
        "https://internal.example.com/path?token=abc",
        "canary.bearer.value-123",
        "hunter2-canary",
    }
    structured = json.dumps(
        {
            "level": "ERROR",
            "message": (
                f"Auth failed for AKIAEXAMPLECANARY000 in account {ACCOUNT_ID}, contact "
                "oncall@canary-team.example, see https://internal.example.com/path?token=abc, "
                "header Bearer canary.bearer.value-123"
            ),
            "password": "hunter2-canary",
        }
    )
    free_text = f"retry {ACCOUNT_ID} via https://api.example.com/x oncall@canary-team.example"

    result = collect(FakeLogs({"events": [event(1, structured), event(2, free_text)]}))

    dumped = result.model_dump_json()
    assert not any(canary in dumped for canary in canaries)
    assert "[REDACTED:" in dumped


def test_secret_keys_without_a_message_field_are_redacted_in_the_json_summary() -> None:
    raw = json.dumps({"level": "ERROR", "error": "boom", "api_key": "hunter2-canary"})

    [item] = collect(FakeLogs({"events": [event(1, raw)]})).evidence

    assert "hunter2-canary" not in item.summary and "boom" in item.summary


def test_messages_are_sanitized_of_control_characters_and_shortened() -> None:
    long_text = "word " * 200

    cleaned = sanitize_message("line1\nline2\x00\x1b[31mred\ttab")
    shortened = sanitize_message(long_text)

    assert cleaned == "line1 line2 [31mred tab"
    assert len(shortened) <= MAX_MESSAGE_CHARS + 1 and shortened.endswith("…")
    assert shortened.startswith("word word") and "word…" in shortened


# V2-08: disclosure of mixed sources, and the report contract


def investigate(client: FakeLogs, **kwargs):
    registry = build_default_registry()
    registry = ToolRegistry(
        [tool_for(client, **kwargs)]
        + [registry.session()._tools[name] for name in registry.tool_names[1:]]
    )
    service = InvestigationService(DeterministicOrchestrator(), registry)
    request = IncidentRequest.model_validate(
        {
            "service": "payments-api",
            "environment": "production",
            "symptoms": "Elevated 5xx errors",
            "time_range": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"},
        }
    )
    return asyncio.run(service.investigate(request))


def test_live_logs_correlate_with_synthetic_deployments_and_the_mix_is_disclosed() -> None:
    report = investigate(FakeLogs({"events": [event(30), event(31, "plain context line")]}))

    sources = {source.tool: source for source in report.data_sources}
    assert sources["cloudwatch_signals"].origin == "live"
    assert {s.origin for name, s in sources.items() if name != "cloudwatch_signals"} == {"fixture"}
    mixed = [item for item in report.uncertainties if item.description.startswith("Mixed sources")]
    assert len(mixed) == 1
    assert "cloudwatch_signals returned live data" in mixed[0].description
    assert "deployment_history" in mixed[0].description
    # The live ERROR at 10:09:30 follows synthetic deployment d-0042 at 10:05: one signal type
    # (no alarm), so the hypothesis is medium and the report is complete.
    assert report.status == "complete"
    assert report.hypotheses[0].supporting_evidence_ids[0] == "deploy:payments-api:d-0042"
    assert report.hypotheses[0].confidence == "medium"
    assert any(item.kind == "log_context" for item in report.evidence)


def test_fixture_only_reports_do_not_claim_a_mixed_source() -> None:
    from cloud_incident_service.evaluation import load_scenarios

    request = load_scenarios()[0].request
    service = InvestigationService(DeterministicOrchestrator(), build_default_registry())

    report = asyncio.run(service.investigate(request))

    assert {source.origin for source in report.data_sources} == {"fixture"}
    assert not any(item.description.startswith("Mixed sources") for item in report.uncertainties)


def test_truncated_collection_makes_the_report_partial_and_says_so() -> None:
    client = FakeLogs(page(PAGE_SIZE, token="a"), page(50, start=PAGE_SIZE))

    report = investigate(client)

    sources = {source.tool: source.status for source in report.data_sources}
    assert report.status == "partial" and sources["cloudwatch_signals"] == "truncated"
    assert any(
        "cloudwatch_signals stopped at a collection limit" in item.description
        for item in report.uncertainties
    )
    assert report.summary.endswith("Some sources failed, so this assessment is incomplete.")


def test_unavailable_live_source_makes_the_report_partial_without_leaking() -> None:
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"{GROUP} {ACCOUNT_ID}"}}, "x"
    )

    report = investigate(FakeLogs(error=error))

    assert report.status == "partial"
    assert {s.tool: s.status for s in report.data_sources}["cloudwatch_signals"] == "unavailable"
    assert GROUP not in report.model_dump_json() and ACCOUNT_ID not in report.model_dump_json()


def test_report_contract_accepts_the_new_statuses() -> None:
    from cloud_incident_service.domain import DataSource

    assert DataSource(tool="cloudwatch_signals", status="truncated", origin="live").origin == "live"
    assert DataSource(tool="cloudwatch_signals", status="ok").origin == "fixture"
