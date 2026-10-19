from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cloud_incident_service.providers import AIProvider, ProviderConfigurationError, create_provider

app = FastAPI(title="cloud-incident-service", version="0.1.0")


class HealthResponse(BaseModel):
    status: str
    provider: str


class GenerateRequest(BaseModel):
    input: str = Field(min_length=1, max_length=10_000)


class GenerateResponse(BaseModel):
    output: str


@lru_cache(maxsize=1)
def system_prompt() -> str:
    prompt_path = files("cloud_incident_service.prompts").joinpath("system.txt")
    return prompt_path.read_text(encoding="utf-8").strip()


def get_provider() -> AIProvider:
    return create_provider()


@app.exception_handler(ProviderConfigurationError)
async def provider_configuration_error(
    _request: Request, exc: ProviderConfigurationError
) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", provider="configured-at-request-time")


@app.post("/v1/generate", response_model=GenerateResponse)
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


@app.post("/v1/rag", response_model=GenerateResponse)
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
