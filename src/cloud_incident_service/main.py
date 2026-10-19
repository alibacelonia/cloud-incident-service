from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Literal

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cloud_incident_service.application import InvestigationService
from cloud_incident_service.config import ConfigurationError, Settings
from cloud_incident_service.domain import IncidentReport, IncidentRequest
from cloud_incident_service.orchestration import DeterministicOrchestrator
from cloud_incident_service.providers import ProviderInvocationError, create_provider
from cloud_incident_service.tools import ToolRegistry, build_default_registry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fail fast: invalid settings stop startup instead of surfacing on the first request.
    try:
        settings = Settings.from_environment()
        create_provider(settings)
    except ConfigurationError as exc:
        logger.error("%s", exc)
        raise
    logger.info("Configuration valid: provider=%s", settings.provider)
    yield


app = FastAPI(title="cloud-incident-service", version="0.1.0", lifespan=lifespan)


class HealthResponse(BaseModel):
    status: Literal["ok", "misconfigured"]
    provider: str | None
    errors: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str
    code: str


# Configuration and provider failures map to safe error bodies (NFR-8). No route invokes a
# provider until model-driven investigations arrive (F-09); the mapping is kept and tested.
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


@app.get("/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
def health(response: Response) -> HealthResponse:
    # Checks settings and provider construction only; no network call, so credentials
    # are verified on the first provider invocation.
    try:
        settings = Settings.from_environment()
        create_provider(settings)
    except ConfigurationError as exc:
        response.status_code = 503
        return HealthResponse(status="misconfigured", provider=None, errors=exc.errors)
    return HealthResponse(status="ok", provider=settings.provider)


@lru_cache(maxsize=1)
def default_registry() -> ToolRegistry:
    return build_default_registry()


def get_investigation_service() -> InvestigationService:
    return InvestigationService(DeterministicOrchestrator(), default_registry())


@app.post("/v1/investigations", response_model=IncidentReport)
async def create_investigation(
    request: IncidentRequest,
    service: InvestigationService = Depends(get_investigation_service),
) -> IncidentReport:
    return await service.investigate(request)
