from datetime import UTC, datetime, timedelta

from cloud_incident_service.correlation import (
    abnormal_signals,
    correlated_changes,
    find_contradictions,
    signal_onset,
    stale_tools,
)
from cloud_incident_service.domain import Evidence, ToolResult

BASE = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


def ev(source_id: str, kind: str, minute: float, **attributes) -> Evidence:
    return Evidence(
        source_id=source_id,
        tool="cloudwatch_signals",
        service="payments-api",
        environment="production",
        timestamp=BASE + timedelta(minutes=minute),
        kind=kind,
        summary=source_id,
        attributes=attributes,
    )


ALARM = ev("alarm:payments-api:a1", "alarm_state_change", 20, new_state="ALARM")
RECOVERY = ev("alarm:payments-api:a0", "alarm_state_change", 5, new_state="OK")
ERROR_LOG = ev("log:payments-api:l1", "log_event", 25, level="ERROR")
WARN_LOG = ev("log:payments-api:l0", "log_event", 2, level="WARN")
METRIC = ev("metric:payments-api:m1", "metric_datapoint", 1, value=99.0)


def test_only_alarms_entering_alarm_and_error_logs_are_abnormal() -> None:
    evidence = [METRIC, WARN_LOG, RECOVERY, ERROR_LOG, ALARM]

    assert abnormal_signals(evidence) == [ALARM, ERROR_LOG]
    assert signal_onset(evidence) == ALARM


def test_onset_ties_break_on_source_id() -> None:
    first = ev("alarm:payments-api:a-first", "alarm_state_change", 20, new_state="ALARM")

    # Same timestamp: "alarm:payments-api:a-first" sorts before "alarm:payments-api:a1".
    assert signal_onset([ALARM, first]) == first


def test_no_abnormal_signals_means_no_onset() -> None:
    assert signal_onset([METRIC, WARN_LOG, RECOVERY]) is None


def test_changes_correlate_within_thirty_minutes_before_onset() -> None:
    onset = ev("alarm:payments-api:on", "alarm_state_change", 40, new_state="ALARM")
    at_boundary = ev("deploy:payments-api:d1", "deployment", 10)
    too_early = ev("deploy:payments-api:d0", "deployment", 9.9)
    same_time = ev("change:payments-api:c1", "infrastructure_change", 40)
    after = ev("change:payments-api:c2", "infrastructure_change", 41)

    result = correlated_changes([after, same_time, too_early, at_boundary, ALARM], onset)

    assert result == [at_boundary, same_time]


def test_contradiction_requires_same_entity_with_different_states() -> None:
    deploy = ev(
        "deploy:payments-api:d1", "deployment", 0, entity="deployment:d1", entity_state="succeeded"
    )
    log = ev(
        "log:payments-api:l9", "log_event", 5, entity="deployment:d1", entity_state="rolled_back"
    )
    agreeing = ev(
        "deploy:payments-api:d2", "deployment", 0, entity="deployment:d2", entity_state="failed"
    )
    agreeing_log = ev(
        "log:payments-api:l8", "log_event", 5, entity="deployment:d2", entity_state="failed"
    )

    contradictions = find_contradictions([log, deploy, agreeing, agreeing_log, METRIC])

    assert len(contradictions) == 1
    assert contradictions[0].entity == "deployment:d1"
    assert contradictions[0].source_ids == ["deploy:payments-api:d1", "log:payments-api:l9"]
    assert contradictions[0].conflicting_with(deploy) == [log]


def result(
    status: str, data_as_of: datetime | None, tool: str = "cloudwatch_signals"
) -> ToolResult:
    return ToolResult(tool=tool, status=status, data_as_of=data_as_of)


def test_stale_means_data_ends_before_the_time_range() -> None:
    end = BASE + timedelta(hours=1)

    assert stale_tools([result("ok", end - timedelta(seconds=1))], end) == {"cloudwatch_signals"}
    assert stale_tools([result("empty", end - timedelta(minutes=5))], end) == {"cloudwatch_signals"}
    assert stale_tools([result("ok", end)], end) == set()
    assert stale_tools([result("unavailable", None)], end) == set()
