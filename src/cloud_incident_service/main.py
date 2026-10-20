from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from cloud_incident_service.api_security import API_KEY_HEADER, ApiGuardMiddleware
from cloud_incident_service.application import InvestigationService
from cloud_incident_service.config import ConfigurationError, Settings
from cloud_incident_service.domain import IncidentReport, IncidentRequest
from cloud_incident_service.orchestration import ModelInvestigationError, build_orchestrator
from cloud_incident_service.providers import ProviderInvocationError
from cloud_incident_service.tools import ToolRegistry, build_default_registry
from cloud_incident_service.tracing import FileTraceSink, check_trace_directory

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fail fast: invalid settings stop startup instead of surfacing on the first request.
    try:
        settings = Settings.from_environment()
        build_orchestrator(settings)
        registry_for(settings)
        if settings.trace_dir is not None:
            check_trace_directory(settings.trace_dir, create=True)
    except ConfigurationError as exc:
        logger.error("%s", exc)
        raise
    logger.info(
        "Configuration valid: provider=%s orchestrator=%s", settings.provider, settings.orchestrator
    )
    if settings.log_source == "cloudwatch":
        logger.warning(
            "LOG_SOURCE=cloudwatch: cloudwatch_signals reads live logs from %d allowlisted log "
            "group(s); other tools remain synthetic fixtures.",
            len(settings.cloudwatch_log_groups),
        )
    if settings.service_api_key is None:
        logger.warning(
            "SERVICE_API_KEY is not set: the API is unauthenticated. Keep it bound to 127.0.0.1."
        )
    else:
        logger.info("API-key authentication is enabled")
    yield


app = FastAPI(title="cloud-incident-service", version="0.1.0", lifespan=lifespan)
app.add_middleware(ApiGuardMiddleware)
# Documents the header in the OpenAPI schema; ApiGuardMiddleware enforces it (ADR-017).
api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False, scheme_name="ApiKeyAuth")


class HealthResponse(BaseModel):
    status: Literal["ok", "misconfigured"]
    provider: str | None
    errors: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str
    code: str


# Configuration, provider, and model-loop failures map to safe error bodies (NFR-8, D-7).
@app.exception_handler(ConfigurationError)
async def configuration_error(_request: Request, exc: ConfigurationError) -> JSONResponse:
    body = ErrorResponse(detail=str(exc), code="configuration_invalid")
    return JSONResponse(status_code=503, content=body.model_dump())


@app.exception_handler(ProviderInvocationError)
async def provider_invocation_error(
    _request: Request, exc: ProviderInvocationError
) -> JSONResponse:
    body = ErrorResponse(detail=exc.message, code=exc.code)
    return JSONResponse(status_code=503 if exc.unavailable else 502, content=body.model_dump())


_MODEL_ERROR_STATUS = {"invalid_output": 502, "limit_exceeded": 502, "timeout": 504}


@app.exception_handler(ModelInvestigationError)
async def model_investigation_error(
    _request: Request, exc: ModelInvestigationError
) -> JSONResponse:
    body = ErrorResponse(detail=exc.message, code=exc.code)
    return JSONResponse(status_code=_MODEL_ERROR_STATUS[exc.kind], content=body.model_dump())


@app.get("/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
def health(response: Response) -> HealthResponse:
    # Checks settings, provider construction, and the trace directory; no network call, so
    # credentials are verified on the first provider invocation.
    try:
        settings = Settings.from_environment()
        build_orchestrator(settings)
        registry_for(settings)
        if settings.trace_dir is not None:
            check_trace_directory(settings.trace_dir, create=False)
    except ConfigurationError as exc:
        response.status_code = 503
        return HealthResponse(status="misconfigured", provider=None, errors=exc.errors)
    return HealthResponse(status="ok", provider=settings.provider)


_registries: dict[tuple[object, ...], ToolRegistry] = {}


def registry_for(settings: Settings) -> ToolRegistry:
    """One registry per tool configuration, so a live AWS client is built once, not per request."""
    key = (
        settings.log_source,
        tuple(sorted(settings.cloudwatch_log_groups.items())),
        settings.aws_region,
    )
    if key not in _registries:
        _registries[key] = build_default_registry(settings)
    return _registries[key]


def get_investigation_service() -> InvestigationService:
    settings = Settings.from_environment()
    sink = FileTraceSink(settings.trace_dir) if settings.trace_dir is not None else None
    return InvestigationService(
        build_orchestrator(settings), registry_for(settings), trace_sink=sink
    )


@app.post(
    "/v1/investigations",
    response_model=IncidentReport,
    responses={code: {"model": ErrorResponse} for code in (401, 413, 502, 503, 504)},
    dependencies=[Security(api_key_scheme)],
)
async def create_investigation(
    request: IncidentRequest,
    service: InvestigationService = Depends(get_investigation_service),
) -> IncidentReport:
    return await service.investigate(request)
