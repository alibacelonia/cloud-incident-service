from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cloud_incident_service.config import ConfigurationError, Settings
from cloud_incident_service.providers import AIProvider, ProviderInvocationError, create_provider

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


class GenerateRequest(BaseModel):
    input: str = Field(min_length=1, max_length=10_000)


class GenerateResponse(BaseModel):
    output: str


@lru_cache(maxsize=1)
def system_prompt() -> str:
    prompt_path = files("cloud_incident_service.prompts").joinpath("system.txt")
    return prompt_path.read_text(encoding="utf-8").strip()


def get_settings() -> Settings:
    return Settings.from_environment()


def get_provider(settings: Settings = Depends(get_settings)) -> AIProvider:
    return create_provider(settings)


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


_PROVIDER_ERRORS = {502: {"model": ErrorResponse}, 503: {"model": ErrorResponse}}


@app.post("/v1/generate", response_model=GenerateResponse, responses=_PROVIDER_ERRORS)
async def generate(
    request: GenerateRequest,
    provider: AIProvider = Depends(get_provider),
) -> GenerateResponse:
    rendered = f"{system_prompt()}\n\nUser input:\n{request.input}"
    return GenerateResponse(output=await provider.generate(rendered))


class DocumentRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)


class DocumentResponse(BaseModel):
    document_id: str
    stored: bool


document_store: dict[str, str] = {}


def retrieve_context(query: str, limit: int = 3) -> list[str]:
    terms = set(query.lower().split())
    ranked = sorted(
        document_store.values(),
        key=lambda text: len(terms.intersection(text.lower().split())),
        reverse=True,
    )
    return [text for text in ranked if terms.intersection(text.lower().split())][:limit]


@app.post("/v1/documents", response_model=DocumentResponse)
def store_document(request: DocumentRequest) -> DocumentResponse:
    document_store[request.document_id] = request.text
    return DocumentResponse(document_id=request.document_id, stored=True)


@app.post("/v1/rag", response_model=GenerateResponse, responses=_PROVIDER_ERRORS)
async def generate_with_retrieval(
    request: GenerateRequest,
    provider: AIProvider = Depends(get_provider),
) -> GenerateResponse:
    context = "\n\n".join(retrieve_context(request.input)) or "No matching context."
    rendered = f"{system_prompt()}\n\nContext:\n{context}\n\nUser input:\n{request.input}"
    return GenerateResponse(output=await provider.generate(rendered))


class DocumentPreview(BaseModel):
    characters: int
    words: int


@app.post("/v1/documents/preview", response_model=DocumentPreview)
def preview_document(request: GenerateRequest) -> DocumentPreview:
    return DocumentPreview(characters=len(request.input), words=len(request.input.split()))
