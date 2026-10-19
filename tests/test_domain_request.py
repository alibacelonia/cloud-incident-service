from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cloud_incident_service.domain import IncidentRequest


def request_data(**overrides) -> dict:
    data = {
        "service": "payments-api",
        "environment": "production",
        "symptoms": "Elevated 5xx errors after 10:05 UTC",
        "time_range": {"start": "2026-10-19T10:00:00Z", "end": "2026-10-19T11:00:00Z"},
    }
    data.update(overrides)
    return data


def test_valid_request_normalizes_timestamps_to_utc() -> None:
    request = IncidentRequest.model_validate(
        request_data(
            time_range={"start": "2026-10-19T18:00:00+08:00", "end": "2026-10-19T19:30:00+08:00"}
        )
    )

    assert request.time_range.start == datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    assert request.time_range.start.utcoffset().total_seconds() == 0
    assert request.time_range.end == datetime(2026, 10, 19, 11, 30, tzinfo=UTC)


def test_symptoms_are_trimmed() -> None:
    assert IncidentRequest.model_validate(request_data(symptoms="  slow  ")).symptoms == "slow"


def test_window_of_exactly_24_hours_is_accepted() -> None:
    IncidentRequest.model_validate(
        request_data(time_range={"start": "2026-10-18T11:00:00Z", "end": "2026-10-19T11:00:00Z"})
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"service": ""},
        {"service": "Payments-API"},
        {"service": "-payments"},
        {"service": "payments_api"},
        {"service": "a" * 65},
        {"environment": "prod"},
        {"symptoms": ""},
        {"symptoms": "   "},
        {"symptoms": "x" * 2_001},
        {"unexpected": "field"},
        {"time_range": {"start": "2026-10-19T10:00:00", "end": "2026-10-19T11:00:00Z"}},
        {"time_range": {"start": "2026-10-19T11:00:00Z", "end": "2026-10-19T11:00:00Z"}},
        {"time_range": {"start": "2026-10-19T12:00:00Z", "end": "2026-10-19T11:00:00Z"}},
        {"time_range": {"start": "2026-10-18T10:59:59Z", "end": "2026-10-19T11:00:00Z"}},
        {"time_range": {"start": 1_760_868_000, "end": "2026-10-19T11:00:00Z"}},
        {"time_range": {"start": "2026-10-19T10:00:00Z"}},
        {
            "time_range": {
                "start": "2026-10-19T10:00:00Z",
                "end": "2026-10-19T11:00:00Z",
                "extra": "x",
            }
        },
    ],
    ids=[
        "empty-service",
        "uppercase-service",
        "leading-hyphen-service",
        "underscore-service",
        "long-service",
        "unknown-environment",
        "empty-symptoms",
        "blank-symptoms",
        "long-symptoms",
        "unknown-field",
        "naive-timestamp",
        "end-equals-start",
        "end-before-start",
        "window-over-24h",
        "numeric-timestamp",
        "missing-end",
        "unknown-time-range-field",
    ],
)
def test_invalid_requests_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        IncidentRequest.model_validate(request_data(**overrides))
