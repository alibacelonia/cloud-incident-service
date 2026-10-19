from __future__ import annotations

import json
from datetime import datetime, timedelta
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from cloud_incident_service.domain.request import Environment, ServiceName
from cloud_incident_service.tools.base import IncidentWindowQuery, ToolOutput, ToolUnavailableError

RecordId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")]


class FixtureRecord(BaseModel):
    """Fields every synthetic record shares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: RecordId
    service: ServiceName
    environment: Environment


class FixtureTool:
    """Read-only tool backed by a packaged JSON fixture (ADR-004).

    The fixture holds `data_as_of` (optionally overridden per `service/environment` key in
    `data_as_of_overrides`), an optional `unavailable` list of `service/environment` keys that
    simulate an outage, and one list per collection. Records are filtered by
    service, environment, and time window; records whose timestamp cannot be parsed are kept
    so that output validation reports them as malformed instead of silently dropping them.
    """

    name: ClassVar[str]
    output_model: ClassVar[type[ToolOutput]]
    # Collection name -> timestamp field used for window filtering.
    collections: ClassVar[dict[str, str]]
    lookback: ClassVar[timedelta] = timedelta(0)

    read_only: Literal[True] = True
    input_model = IncidentWindowQuery

    def __init__(self, fixture: Traversable | None = None) -> None:
        self._fixture = fixture or files("cloud_incident_service.fixtures").joinpath(
            f"{self.name}.json"
        )

    async def run(self, query: IncidentWindowQuery) -> dict[str, Any]:
        data = json.loads(self._fixture.read_text(encoding="utf-8"))
        scope = f"{query.service}/{query.environment}"
        if scope in data.get("unavailable", []):
            raise ToolUnavailableError(f"{self.name} is unavailable for this service")

        window_start = query.start - self.lookback
        data_as_of = data.get("data_as_of_overrides", {}).get(scope, data.get("data_as_of"))
        output: dict[str, Any] = {"data_as_of": data_as_of}
        for collection, timestamp_field in self.collections.items():
            output[collection] = [
                record
                for record in data.get(collection, [])
                if record.get("service") == query.service
                and record.get("environment") == query.environment
                and _within(record.get(timestamp_field), window_start, query.end)
            ]
        return output


def _within(raw: object, start: datetime, end: datetime) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if timestamp.tzinfo is None:
        return True
    return start <= timestamp <= end
