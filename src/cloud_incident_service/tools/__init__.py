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
from cloud_incident_service.tools.runbook_search import RunbookSearchQuery, RunbookSearchTool
from cloud_incident_service.tools.runbooks import (
    LexicalRunbookRetriever,
    RunbookRetriever,
    load_runbooks,
)


def build_default_registry() -> ToolRegistry:
    """The MVP's fixture-backed, read-only tools."""
    return ToolRegistry(
        [
            CloudWatchSignalsTool(),
            DeploymentHistoryTool(),
            InfrastructureChangesTool(),
            RunbookSearchTool(),
        ]
    )


__all__ = [
    "CloudWatchSignalsTool",
    "DeploymentHistoryTool",
    "IncidentWindowQuery",
    "InfrastructureChangesTool",
    "LexicalRunbookRetriever",
    "RunbookRetriever",
    "RunbookSearchQuery",
    "RunbookSearchTool",
    "Tool",
    "ToolInputError",
    "ToolNotRegisteredError",
    "ToolOutput",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolSession",
    "ToolUnavailableError",
    "build_default_registry",
    "load_runbooks",
]
