"""Live CloudWatch Logs read. One bounded, read-only call set; no model is involved (V2-10).

Run against a sandbox log group of synthetic logs you are authorized to read:
    RUN_LIVE_TESTS=1 RUN_LIVE_LOG_TESTS=1 AWS_REGION=<region> \\
    CLOUDWATCH_LOG_GROUPS='{"<service>/<environment>": "<log-group-name>"}' \\
    LIVE_LOG_SERVICE=<service> LIVE_LOG_ENVIRONMENT=<environment> \\
    uv run pytest -m live -s tests/live/test_live_cloudwatch.py
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from live_support import DEFAULT_LIVE_LOG_WINDOW_MINUTES

from cloud_incident_service.config import Settings
from cloud_incident_service.redaction import redact_text
from cloud_incident_service.tools import CloudWatchLogsTool, ToolRegistry
from cloud_incident_service.tools.cloudwatch_logs import (
    MAX_EVENTS,
    MAX_MESSAGE_CHARS,
    create_logs_client,
)

pytestmark = pytest.mark.live


def test_live_log_read_is_bounded_sanitized_and_scoped(live_log_environment) -> None:
    env = live_log_environment
    settings = Settings.from_environment(
        {**env, "LOG_SOURCE": "cloudwatch", "ORCHESTRATOR": "deterministic", "AI_PROVIDER": "mock"}
    )
    service = env["LIVE_LOG_SERVICE"].strip()
    environment = env["LIVE_LOG_ENVIRONMENT"].strip()
    minutes = int(env.get("LIVE_LOG_WINDOW_MINUTES", DEFAULT_LIVE_LOG_WINDOW_MINUTES))
    end = datetime.now(UTC)
    arguments = {
        "service": service,
        "environment": environment,
        "start": (end - timedelta(minutes=minutes)).isoformat(),
        "end": end.isoformat(),
    }
    client = create_logs_client(settings.aws_region)
    calls: list[dict] = []
    real = client.filter_log_events

    def counting(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    client.filter_log_events = counting
    tool = CloudWatchLogsTool(settings.cloudwatch_log_groups, client)

    result = asyncio.run(ToolRegistry([tool]).session().invoke("cloudwatch_signals", arguments))

    print(
        f"\nlive log read: status={result.status} events={len(result.evidence)} calls={len(calls)}"
    )
    assert result.status in {"ok", "empty", "truncated"}, result.error
    assert result.origin == "live"
    assert 1 <= len(calls) <= 3
    assert len(result.evidence) <= MAX_EVENTS
    assert {call["logGroupName"] for call in calls} == {
        settings.cloudwatch_log_groups[f"{service}/{environment}"]
    }
    for item in result.evidence:
        assert (item.service, item.environment) == (service, environment)
        assert item.source_id.startswith(f"log:{service}:cw-")
        assert len(item.summary) <= MAX_MESSAGE_CHARS + 32
        assert redact_text(item.summary) == item.summary
        assert "http://" not in item.summary and "https://" not in item.summary
