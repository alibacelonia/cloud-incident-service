from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from cloud_incident_service.config import ConfigurationError
from cloud_incident_service.tracing.models import InvestigationTrace


class TraceSink(Protocol):
    def write(self, trace: InvestigationTrace) -> None: ...


class InMemoryTraceSink:
    """Keeps traces in memory; used by tests."""

    def __init__(self) -> None:
        self.traces: list[InvestigationTrace] = []

    def write(self, trace: InvestigationTrace) -> None:
        self.traces.append(trace)


class FileTraceSink:
    """Writes one JSON file per investigation, named `<investigation_id>.json` (D-3)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def write(self, trace: InvestigationTrace) -> None:
        target = self.directory / f"{trace.investigation_id}.json"
        # Write to a temporary file first so readers never see a partial trace.
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(trace.model_dump_json(indent=2))
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def check_trace_directory(directory: Path, *, create: bool) -> None:
    """Validate TRACE_DIR (NFR-8). Messages name the variable, never the path."""
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ConfigurationError(["TRACE_DIR: directory could not be created"]) from None
    if not directory.is_dir():
        raise ConfigurationError(["TRACE_DIR: directory does not exist"])
    if not os.access(directory, os.W_OK | os.X_OK):
        raise ConfigurationError(["TRACE_DIR: directory is not writable"])
