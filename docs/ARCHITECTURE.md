# Architecture

This document describes the implemented MVP (features F-01 to F-11 in `docs/TASKS.md`). The
requirements are in `docs/PROJECT.md` and the reasoning behind each choice is in
`docs/DECISIONS.md`.

## Technology stack

- Python 3.12+, FastAPI, Pydantic, pytest, Ruff, and uv.
- boto3, used only by the opt-in Amazon Bedrock adapter.
- No database, background workers, Docker, or frontend (`project.yaml`).

## Components

```text
FastAPI boundary (main.py)
    -> InvestigationService (application/)
        -> redacts symptoms, opens a ToolSession, records the trace
        -> Orchestrator protocol (orchestration/)
            -> DeterministicOrchestrator (default) -> findings.build_findings -> correlation/
            -> ModelOrchestrator (ORCHESTRATOR=model, bounded loop)
                -> ModelClient protocol (providers/)
                    -> BedrockModelClient (AI_PROVIDER=bedrock, Converse with tool use)
                    -> RuleBasedModelClient (AI_PROVIDER=mock, offline stand-in)
            -> ToolSession -> ToolRegistry (read-only tools only, tools/)
                -> cloudwatch_signals      (fixture-backed)
                -> deployment_history      (fixture-backed, 60-minute look-back)
                -> infrastructure_changes  (fixture-backed, 60-minute look-back)
                -> runbook_search          (Markdown runbooks, lexical RunbookRetriever)
        -> IncidentReport (domain/, validated)
        -> TraceRecorder -> redaction -> TraceSink (JSON file when TRACE_DIR is set, tracing/)
Evaluation runner (evaluation/) -> InvestigationService over fixtures/evaluation_scenarios.json
```

| Module | Responsibility |
| --- | --- |
| `main.py` | `GET /health` and `POST /v1/investigations`; dependency wiring; startup validation (lifespan); mapping of typed errors to HTTP responses |
| `config.py` | `Settings` from environment variables; `ConfigurationError` names variables, never values (ADR-008) |
| `domain/` | Request, evidence, tool result, and report models; FR-5 cross-reference validators. `Findings` is what an orchestrator concludes; `IncidentReport` adds identity, request, provenance, and request-scoped checks |
| `application/` | `InvestigationService`: redacts symptoms, opens a `ToolSession` per investigation, assembles and validates the report, rejects evidence no tool returned (ADR-009), records and exports the trace |
| `orchestration/` | `Orchestrator` protocol; `DeterministicOrchestrator` with rule-based findings (ADR-010); `ModelOrchestrator` (ADR-013) whose hypotheses are checked against the correlation rules in `grounding.py` (ADR-015); `RuleBasedModelClient`; `build_orchestrator` |
| `correlation/` | Pure rules: abnormal signals and onset, changes within 30 minutes before onset (A-3), stale sources (A-4), contradictions via shared `entity` attributes (A-5), instruction-like text (ADR-012) |
| `tools/` | `Tool` protocol, `ToolRegistry`, `ToolSession`, the four fixture-backed tools (D-8), runbook parsing and retrieval (D-1, ADR-007) |
| `providers/` | `ModelClient` protocol and conversation types; `BedrockModelClient` with timeouts and safe error mapping |
| `prompts/` | Versioned prompt templates; `investigation_v4.txt` is current, earlier versions are kept for comparison |
| `tracing/` | `InvestigationTrace`, `TraceRecorder`, `FileTraceSink`, `InMemoryTraceSink`, `TRACE_DIR` checks (D-3, ADR-006, ADR-011) |
| `redaction.py` | Pattern-based secret redaction for tool output, symptoms, and traces (ADR-011) |
| `fixtures/` | Synthetic tool data, runbooks, and evaluation scenarios (package data, hygiene-tested) |
| `evaluation/` | Scenario runner and CLI, `python -m cloud_incident_service.evaluation` (ADR-012) |

### Dependency rules

- `domain` imports nothing from other project packages.
- `correlation` depends only on `domain`.
- `tools`, `orchestration`, `tracing`, and `application` depend on `domain`, on each other's
  protocols, and on `redaction`, never on boto3.
- Only `providers/bedrock.py` imports boto3 and botocore, and it is imported only when
  `AI_PROVIDER=bedrock`.
- `main.py` is the only module that knows about HTTP. Lower layers raise typed errors
  (`ConfigurationError`, `ProviderInvocationError`, `ModelInvestigationError`) that `main.py` maps
  to status codes.

### Key interfaces

- `Tool`: `name`, `description`, `read_only: Literal[True]`, `input_model`, `output_model`, and
  `async run(query) -> raw output`. The session validates raw output against `output_model`, a
  `ToolOutput` that converts its records to evidence.
- `ToolRegistry`: `register(tool)` rejects non-read-only and duplicate tools; `session(observer)`
  opens a `ToolSession` for one investigation.
- `ToolSession`: `invoke(name, arguments) -> ToolResult` refuses unknown names and invalid arguments
  without running anything, enforces a 5-second per-tool timeout, turns exceptions into
  `unavailable` and invalid output into `malformed`, redacts evidence, and records results,
  returned evidence, and trace events. `specs()` describes the tools for model tool use.
- `Orchestrator`: `provenance` plus `async investigate(request, tools: ToolSession) -> Findings`.
- `ModelClient`: `model_id` plus `async converse(system, messages, tools, max_output_tokens) ->
  ModelTurn`, where `ModelTurn` holds text and tool-use blocks, a normalized stop reason, and token
  usage.
- `RunbookRetriever`: `data_as_of` plus `search(service, query, limit) -> list[RankedSection]`.
- `TraceSink`: `write(trace)`.

## Data flow

1. The API validates the `IncidentRequest` (FR-1). Invalid input returns 422 before anything runs.
2. `InvestigationService` redacts the symptoms, assigns an investigation ID, starts a
   `TraceRecorder`, and opens a `ToolSession` that reports to it.
3. The orchestrator collects evidence through the session. The deterministic orchestrator calls
   each tool once; the model orchestrator lets the model request registered tools within 8 tool
   calls, 6 turns, and 60 seconds (A-7). Every result is validated, redacted, and traced.
4. Findings are produced. The deterministic path applies the correlation rules. On the model path
   the model writes the analysis and cites evidence by ID, while the orchestrator supplies the
   evidence, timeline, source statuses, and source uncertainties; invalid answers get one repair
   attempt (D-7).
5. The service validates the `IncidentReport`, including request scope and cross-references, and
   checks that every evidence item matches what a tool returned in this session.
6. The trace is completed, redacted, and written when `TRACE_DIR` is set. The report is returned.

## Error handling

| Situation | Result |
| --- | --- |
| Invalid request | 422 with field errors; nothing executed |
| One or more tools unavailable or malformed | 200 with `status: partial` |
| Invalid settings | Startup fails; `/health` returns 503 `misconfigured`; requests return 503 `configuration_invalid` |
| Provider unavailable (credentials, access, throttling, connectivity) | 503 with a stable `provider_*` code |
| Provider rejected the request or returned an unexpected response | 502 with a stable `provider_*` code |
| Model output invalid after one repair attempt | 502 `invalid_model_output` |
| Model turn limit reached | 502 `model_turn_limit` |
| Model loop exceeded 60 seconds | 504 `model_timeout` |
| Trace could not be written | Logged by investigation ID and error type; the report is still returned |

## Security

See `docs/SECURITY.md` for the full posture. In short: tools are read-only by construction,
remediation is never executed, tool output is untrusted data, the model cannot introduce evidence,
secrets are redacted where untrusted text enters, fixtures are synthetic, Bedrock access is limited
to `bedrock:InvokeModel` on one model (`docs/iam/`), and the API has no authentication, so it must
stay on `127.0.0.1` (D-5).

## Testing strategy

- **Unit:** domain validators, correlation rules, registry behaviour, runbook parsing and ranking,
  redaction, and the Bedrock request and response mapping with a fake client.
- **Integration:** API tests through `TestClient`, the deterministic orchestrator over fixtures, and
  the model orchestrator with scripted model turns (limits, refusals, repair, timeouts, injection).
- **Evaluation:** ten scenarios scored on schema, citations, hypothesis, sources, and safety. Both
  orchestrators must pass 100% offline; CI runs both.
- **Documentation:** `tests/test_docs.py` checks the README example request, the IAM policies, the
  security controls table, and that every setting is documented.
- **Live:** `tests/live/`, marked `live` and deselected by default. With `-m live` they still skip
  with a reason unless `RUN_LIVE_TESTS=1`, `AI_MODEL`, `AWS_REGION`, and credentials are present.
  They invoke only Bedrock, are bounded by output-token caps, the loop limits, and per-test
  timeouts, and assert the D-6 threshold of 80%.
- A socket-blocking guard and environment isolation in `tests/conftest.py` keep default tests
  offline and independent of the developer's shell.

## Deployment approach

Run with uvicorn on `127.0.0.1`. Amazon Bedrock is opt-in. Cloud deployment, real AWS data sources,
a vector store, and background workers are out of scope for the MVP; any non-local deployment first
requires authentication and the other prerequisites in `docs/SECURITY.md`.
