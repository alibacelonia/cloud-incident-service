from cloud_incident_service.tools.base import (
    IncidentWindowQuery,
    Tool,
    ToolInputError,
    ToolNotRegisteredError,
    ToolOutput,
    ToolRegistrationError,
    ToolUnavailableError,
)
from cloud_incident_service.tools.cloudwatch import CloudWatchSignalsTool
from cloud_incident_service.tools.deployments import DeploymentHistoryTool
from cloud_incident_service.tools.infrastructure import InfrastructureChangesTool
from cloud_incident_service.tools.registry import ToolRegistry, ToolSession


def build_default_registry() -> ToolRegistry:
    """The MVP's fixture-backed, read-only tools."""
    return ToolRegistry(
        [CloudWatchSignalsTool(), DeploymentHistoryTool(), InfrastructureChangesTool()]
    )


__all__ = [
    "CloudWatchSignalsTool",
    "DeploymentHistoryTool",
    "IncidentWindowQuery",
    "InfrastructureChangesTool",
    "Tool",
    "ToolInputError",
    "ToolNotRegisteredError",
    "ToolOutput",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolSession",
    "ToolUnavailableError",
    "build_default_registry",
]
