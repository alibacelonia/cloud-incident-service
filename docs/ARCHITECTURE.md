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

- `main.py` exposes only `GET /health` and `POST /v1/investigations`. It defines HTTP boundaries, dependency injection, startup validation (lifespan), the
  `/health` readiness check, and error mapping.
- `config.py` validates settings from the environment and raises `ConfigurationError` (ADR-008).
- `domain/` holds the incident request and report models. `Findings` is what an orchestrator
  concludes; `IncidentReport` extends it with identity, request, and provenance, and adds the
  request-scoped checks. Cross-reference rules (FR-5) are model validators.
- `application/` contains `InvestigationService`, which opens a `ToolSession` per investigation,
  assigns the investigation ID and timestamp, assembles the validated report, and rejects evidence
  that no tool returned in that session (FR-5 rule 1, ADR-009).
- `orchestration/` defines the `Orchestrator` protocol and a `DeterministicOrchestrator` that
  queries every registered tool once and reports evidence, timeline, data-source status, and
  uncertainties for empty or failed sources. Correlated findings arrive in F-06.
- `tools/` holds the read-only `ToolRegistry` and `ToolSession` and four fixture-backed tools:
  `cloudwatch_signals`, `deployment_history`, `infrastructure_changes`, and `runbook_search` (D-8).
  Change tools apply the 60-minute look-back (A-3). `runbooks.py` parses Markdown runbooks into
  sections (`runbook:<id>:<section>`) and ranks them with a deterministic lexical retriever (D-1,
  ADR-007): matched query terms score 1 in the title, 2 in the heading, and 1 in the body; ties go
  to service-specific runbooks, then source ID. The deterministic orchestrator searches runbooks with
  the reported symptoms and keeps runbook sections out of the event timeline.
- `fixtures/` contains the synthetic JSON data, checked by a hygiene test (SEC-5).
- `providers/` defines the provider protocol (`AIProvider.generate(prompt) -> str`), an offline mock,
  and the Bedrock adapter in `providers/bedrock.py`, which translates SDK failures into
  `ProviderInvocationError`.
- `prompts/` stores prompt text outside Python code.

Known gaps against the target: no correlation, trace, or evaluation harness. The provider protocol supports text only, not tool use.

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
  `async run(query) -> raw output`. The registry validates the raw output against `output_model`
  (a `ToolOutput` that converts its records to evidence).
- `ToolRegistry`: `register(tool)` rejects non-read-only and duplicate tools; `session()` opens a
  `ToolSession` for one investigation.
- `ToolSession`: `invoke(name, arguments) -> ToolResult` refuses unknown names and invalid arguments
  without running anything, enforces a per-tool timeout (5 s), converts exceptions to
  `unavailable` and invalid output to `malformed`, and records results and returned evidence.
- `Orchestrator`: `provenance` plus `async investigate(request, tools: ToolSession) -> Findings`.
  `InvestigationService` turns findings into an `IncidentReport`.
- `ModelClient`: `async converse(system, messages, tools, limits) -> ModelTurn`, where `ModelTurn`
  is either text or tool-use requests plus token usage. The starter endpoints were removed in F-03 (D-4); `AIProvider`
  now serves only startup and `/health` checks until `ModelClient` replaces it in F-09.
- `RunbookRetriever`: `data_as_of` plus `search(service, query, limit) -> list[RankedSection]`.
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
| Invalid settings | Startup fails; at request time 503 `configuration_invalid` |
| Provider unavailable (credentials, access, throttling, connectivity) | 503 with a stable `code` |
| Provider rejected the request or returned an unexpected response | 502 with a stable `code` |
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
