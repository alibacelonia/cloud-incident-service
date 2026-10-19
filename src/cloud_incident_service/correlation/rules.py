from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from cloud_incident_service.domain import Evidence, ToolResult

# A-3: a change correlates with a signal when it starts at most 30 minutes before the onset.
CORRELATION_WINDOW = timedelta(minutes=30)
CHANGE_KINDS = frozenset({"deployment", "infrastructure_change"})
ENTITY = "entity"
ENTITY_STATE = "entity_state"


def _ordered(evidence: Iterable[Evidence]) -> list[Evidence]:
    return sorted(evidence, key=lambda item: (item.timestamp, item.source_id))


def is_abnormal_signal(item: Evidence) -> bool:
    """Alarms entering ALARM and ERROR log events. Metric values are context, not signals."""
    if item.kind == "alarm_state_change":
        return item.attributes.get("new_state") == "ALARM"
    if item.kind == "log_event":
        return item.attributes.get("level") == "ERROR"
    return False


def abnormal_signals(evidence: Iterable[Evidence]) -> list[Evidence]:
    return _ordered(item for item in evidence if is_abnormal_signal(item))


def signal_onset(evidence: Iterable[Evidence]) -> Evidence | None:
    signals = abnormal_signals(evidence)
    return signals[0] if signals else None


def correlated_changes(
    evidence: Iterable[Evidence], onset: Evidence, window: timedelta = CORRELATION_WINDOW
) -> list[Evidence]:
    return _ordered(
        item
        for item in evidence
        if item.kind in CHANGE_KINDS
        and onset.timestamp - window <= item.timestamp <= onset.timestamp
    )


@dataclass(frozen=True)
class Contradiction:
    """Evidence items that report incompatible states for the same entity (A-5)."""

    entity: str
    evidence: tuple[Evidence, ...]

    @property
    def source_ids(self) -> list[str]:
        return [item.source_id for item in self.evidence]

    def conflicting_with(self, item: Evidence) -> list[Evidence]:
        state = item.attributes.get(ENTITY_STATE)
        return [other for other in self.evidence if other.attributes.get(ENTITY_STATE) != state]


def find_contradictions(evidence: Iterable[Evidence]) -> list[Contradiction]:
    by_entity: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        entity = item.attributes.get(ENTITY)
        if isinstance(entity, str) and item.attributes.get(ENTITY_STATE) is not None:
            by_entity[entity].append(item)
    return [
        Contradiction(entity=entity, evidence=tuple(_ordered(items)))
        for entity, items in sorted(by_entity.items())
        if len({item.attributes[ENTITY_STATE] for item in items}) > 1
    ]


def stale_tools(results: Iterable[ToolResult], window_end: datetime) -> set[str]:
    """A-4: a responding source is stale when its data ends before the time range does."""
    return {
        result.tool
        for result in results
        if result.status in ("ok", "empty")
        and result.data_as_of is not None
        and result.data_as_of < window_end
    }
