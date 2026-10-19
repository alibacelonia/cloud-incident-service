# Architecture

> The **current baseline** section describes code that exists today. The **target MVP architecture**
> sections describe the approved design in `docs/PROJECT.md` (v1) and the accepted ADRs in
> `docs/DECISIONS.md`. It is delivered feature by feature (see `docs/TASKS.md`); parts not yet
> delivered are not implemented.

## Technology stack

- Python 3.12+, FastAPI, Pydantic, pytest, Ruff, and uv.
- boto3, used only for the opt-in Amazon Bedrock adapter.
- No database, background workers, Docker, or frontend (`project.yaml`).

## Current baseline

- `main.py` defines HTTP boundaries and dependency injection.
- `providers/` defines the provider protocol (`AIProvider.generate(prompt) -> str`), an offline mock,
  and the selected Bedrock adapter.
- `prompts/` stores prompt text outside Python code.
- An in-memory retrieval seam provides deterministic local RAG behaviour and can later be replaced
  behind the same boundary.

Known gaps against the target: no tool registry, orchestration, domain models, trace, or evaluation
harness. The provider protocol supports text only, not tool use. `BedrockProvider.generate` calls
synchronous boto3 inside an `async` function, which blocks the event loop (NFR-4).

## Target MVP architecture

```text
FastAPI boundary (main.py / api)
    -> InvestigationService (application)
        -> Orchestrator protocol
            -> DeterministicOrchestrator (default)
            -> ModelOrchestrator (opt-in, bounded loop)  [D-2]
                -> ModelClient protocol
                    -> ScriptedModelClient (offline tests)
                    -> BedrockModelClient (opt-in, Converse with tool use)
            -> ToolRegistry (read-only only)
                -> cloudwatch_signals      (fixture-backed)
                -> deployment_history      (fixture-backed)
                -> infrastructure_changes  (fixture-backed)
                -> runbook_search          (RunbookRetriever, in-memory lexical)  [D-1]
            -> Correlator (normalize, filter, order, correlate, flag stale/contradictions)
        -> IncidentReport (validated domain model)
        -> TraceRecorder -> Redactor -> TraceSink (JSON file)  [D-3]
Evaluation runner (tests/evaluation) -> InvestigationService with scenario fixtures
```

### Proposed module layout

```text
src/cloud_incident_service/
  main.py                 # FastAPI app, routes, dependency wiring, error mapping
  config.py               # Settings from environment
  domain/                 # Pydantic models: request, evidence, tool results, report; no I/O
  application/            # InvestigationService: coordinates orchestrator, trace, validation
  orchestration/          # Orchestrator protocol, deterministic and model-driven implementations
  tools/                  # Tool protocol, ToolRegistry, fixture-backed tools, RunbookRetriever
  correlation/            # Pure functions over evidence
  providers/              # ModelClient protocol, scripted mock, Bedrock adapter
  tracing/                # TraceRecorder, Redactor, TraceSink implementations
  prompts/                # Versioned prompt templates
  fixtures/               # Synthetic scenario data and runbooks (package data)
tests/
  unit/ integration/ evaluation/ live/
```

### Dependency rules

- `domain` imports nothing from other project packages.
- `correlation` depends only on `domain`.
- `tools`, `orchestration`, and `tracing` depend on `domain` and on protocols, never on boto3.
- Only `providers/bedrock` imports boto3, lazily, when Bedrock is selected.
- `main.py` is the only module that knows about HTTP. The application layer raises typed errors
  that `main.py` maps to status codes.

### Key interfaces

- `Tool`: `name`, `read_only: Literal[True]`, `input_model`, `output_model`, and
  `async run(input) -> ToolResult`.
- `ToolRegistry`: `register(tool)` rejects non-read-only tools; `invoke(name, raw_input)` validates
  input, refuses unknown names, enforces a per-tool timeout, and converts exceptions or invalid
  output into `unavailable` or `malformed` results.
- `Orchestrator`: `async investigate(request, registry, recorder) -> IncidentReport`.
- `ModelClient`: `async converse(system, messages, tools, limits) -> ModelTurn`, where `ModelTurn`
  is either text or tool-use requests plus token usage. The existing `AIProvider` remains only while
  the starter endpoints exist [D-4].
- `RunbookRetriever`: `search(query, limit) -> list[RunbookSection]`.
- `TraceSink`: `write(trace)`, with a file-based implementation and an in-memory one for tests.

## Data flow

1. The API validates `IncidentRequest` (FR-1). Invalid input returns 422 before any tool runs.
2. `InvestigationService` creates an `investigation_id` and a `TraceRecorder`, then calls the
   configured orchestrator.
3. The deterministic orchestrator invokes each tool once through the registry. The model
   orchestrator lets the model request registered tools within loop limits (A-7). Every tool result
   is validated and recorded.
4. The correlator normalizes evidence, drops other services and environments, orders the timeline,
   links changes to signal onsets, and flags stale and contradictory data.
5. The report is built (rules or model output) and validated against the `IncidentReport` schema,
   including cross-reference checks on cited evidence identifiers.
6. The trace is redacted and written to the sink. The report is returned.

## Error handling

| Situation | Result |
| --- | --- |
| Invalid request | 422 with field errors; nothing executed |
| One or more tools unavailable or malformed | 200 with `status: partial` |
| Selected provider misconfigured | 503 (existing `ProviderConfigurationError` mapping) |
| Model output invalid after repair attempt | 502 `invalid_model_output` [D-7] |
| Model loop limit or timeout exceeded | 504 or `partial`, to be settled alongside D-7 |

## Security considerations

- Read-only by construction: the registry type requires `read_only: Literal[True]`, and there is no
  code path that executes remediation proposals.
- Prompt-injection containment: tool output is wrapped in explicit data delimiters. The model can
  reach only registered read-only tools. Final reports are validated so facts must cite evidence.
- Secrets: provider credentials come only from the environment or the AWS default chain, never from
  request bodies. Traces pass through a redactor. Prompts and tool payloads are not logged by
  default.
- Bedrock IAM: grant only `bedrock:InvokeModel` (and the streaming variant only if used) on the
  configured model resource.
- Access control: none in the MVP. The service is intended for local use bound to `127.0.0.1` [D-5].
- Fixtures are synthetic and checked by a hygiene test (SEC-5).

## Testing strategy

- **Unit:** domain validators, correlation rules, registry behaviour, redaction.
- **Integration:** API tests through `TestClient` with dependency overrides, plus the deterministic
  orchestrator over fixtures.
- **Evaluation:** scenario runner. The deterministic path must pass 100% of scenarios.
- **Live:** `@pytest.mark.live`, deselected by default and skipped with a reason when not opted in
  or configured.
- A socket-blocking guard in `conftest.py` enforces offline default tests.

## Deployment approach

Run with uvicorn locally. External AI providers, vector stores, workers, and cloud deployment remain
opt-in. Non-local deployment requires the access-control decision (D-5) first.
