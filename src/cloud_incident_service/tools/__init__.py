from cloud_incident_service.config import Settings
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
from cloud_incident_service.tools.cloudwatch_logs import CloudWatchLogsTool, create_logs_client
from cloud_incident_service.tools.deployments import DeploymentHistoryTool
from cloud_incident_service.tools.infrastructure import InfrastructureChangesTool
from cloud_incident_service.tools.registry import ToolCallObserver, ToolRegistry, ToolSession
from cloud_incident_service.tools.runbook_search import RunbookSearchQuery, RunbookSearchTool
from cloud_incident_service.tools.runbooks import (
    LexicalRunbookRetriever,
    RunbookRetriever,
    load_runbooks,
)


def build_default_registry(settings: Settings | None = None) -> ToolRegistry:
    """The read-only tools: fixture-backed unless `LOG_SOURCE=cloudwatch` swaps in live logs.

    Only `cloudwatch_signals` can become live; every other tool stays a synthetic fixture, and
    reports disclose the mix. With the default settings no AWS client is ever constructed.
    """
    signals: CloudWatchSignalsTool | CloudWatchLogsTool = CloudWatchSignalsTool()
    if settings is not None and settings.log_source == "cloudwatch":
        signals = CloudWatchLogsTool(
            settings.cloudwatch_log_groups, create_logs_client(settings.aws_region)
        )
    return ToolRegistry(
        [
            signals,
            DeploymentHistoryTool(),
            InfrastructureChangesTool(),
            RunbookSearchTool(),
        ]
    )


__all__ = [
    "CloudWatchLogsTool",
    "CloudWatchSignalsTool",
    "DeploymentHistoryTool",
    "IncidentWindowQuery",
    "InfrastructureChangesTool",
    "LexicalRunbookRetriever",
    "RunbookRetriever",
    "RunbookSearchQuery",
    "RunbookSearchTool",
    "Tool",
    "ToolCallObserver",
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
