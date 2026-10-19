from __future__ import annotations

from cloud_incident_service.config import Settings
from cloud_incident_service.orchestration.base import Orchestrator
from cloud_incident_service.orchestration.deterministic import DeterministicOrchestrator
from cloud_incident_service.orchestration.model import ModelOrchestrator
from cloud_incident_service.orchestration.simulated import RuleBasedModelClient
from cloud_incident_service.providers import create_model_client


def build_orchestrator(settings: Settings) -> Orchestrator:
    """The configured orchestrator. Constructs, but never calls, the model client."""
    # The provider is constructed even for the deterministic path so misconfiguration is
    # reported at startup and by /health (NFR-8).
    client = create_model_client(settings)
    if settings.orchestrator == "deterministic":
        return DeterministicOrchestrator()
    return ModelOrchestrator(
        client or RuleBasedModelClient(), max_output_tokens=settings.model_max_output_tokens
    )
