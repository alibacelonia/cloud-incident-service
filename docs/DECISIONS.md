# Architecture decisions

## ADR-001: Start from the ai-service profile

- **Status:** accepted
- **Context:** The project needs a lightweight, independently runnable foundation.
- **Decision:** Use AI Project Factory template version 0.2.0 and enable only features recorded in `project.yaml`.
- **Consequences:** The initial system stays small. Later architecture changes must be recorded here with their tradeoffs.

## ADR-002: Single bounded agent behind an orchestration interface

- **Status:** accepted (2026-10-19)
- **Context:** The MVP must investigate incidents with a tool-using agent, stay deterministic in default tests, and avoid a multi-agent design until one agent demonstrably cannot keep clear ownership or context boundaries.
- **Decision:** The API calls an `InvestigationService`, which depends on an `Orchestrator` protocol. Two implementations: a rule-based `DeterministicOrchestrator` (default, built first) and a `ModelOrchestrator` that runs a bounded tool-use loop (8 tool calls, 6 turns, 60 s, output-token cap) through a `ModelClient` protocol. Orchestration code never imports boto3.
- **Consequences:** The deterministic path gives a reproducible baseline for evaluation and for comparison with model behaviour. Two implementations must produce the same `IncidentReport` contract. The deterministic report is template-based and less fluent than model output. Multi-agent orchestration is out of scope.

## ADR-003: Schema-validated report with evidence citations

- **Status:** accepted (2026-10-19)
- **Context:** Responders must be able to tell confirmed facts from guesses, and every factual claim must trace to tool evidence. Model output can be malformed or unsupported.
- **Decision:** `IncidentReport` is a Pydantic model with separate `facts`, `hypotheses`, `uncertainties`, `next_steps`, and `remediation_proposals`. Validators require each fact and hypothesis to cite at least one `source_id` returned during the same investigation, and `requires_human_approval` is `Literal[True]`. Confidence is categorical (`low`, `medium`, `high`) with a rationale (A-6). Invalid model output gets one repair attempt, then fails with 502 (D-7).
- **Consequences:** Unsupported claims are rejected structurally, not by prompt wording alone. Citation checks need the set of evidence returned during the run, so validation happens in the application layer with that context, not as a stateless schema check only. Categorical confidence avoids false precision but cannot express fine-grained ranking.

## ADR-004: Fixture-first, read-only tool registry

- **Status:** accepted (2026-10-19)
- **Context:** Default tests must need no AWS account or model, and the MVP must be unable to change cloud resources.
- **Decision:** Tools implement a typed `Tool` protocol with `read_only: Literal[True]` and Pydantic input and output models. A `ToolRegistry` rejects non-read-only tools at registration, refuses unknown tool names, enforces timeouts, and turns exceptions or invalid output into `unavailable` or `malformed` results. MVP tools read synthetic fixtures packaged with the service. Real AWS read adapters are post-MVP and must implement the same protocol outside the domain layer.
- **Consequences:** Behaviour is reproducible and safe to demo publicly. Fixture realism limits how far evaluation results generalise. Any future side-effecting capability needs a new ADR and a human-approval design before registration is possible.

## ADR-005: Tool-use capable model client protocol

- **Status:** accepted (2026-10-19); implemented in F-09, where `AIProvider` was removed rather than kept alongside (see ADR-013)
- **Context:** The existing `AIProvider.generate(prompt) -> str` cannot express tool specifications, tool-use requests, or token usage. The Bedrock adapter blocks the event loop by calling synchronous boto3 from an `async` method.
- **Decision:** Add a `ModelClient` protocol (`converse(system, messages, tools, limits) -> ModelTurn`) with a scripted offline implementation for tests and a Bedrock Converse implementation that runs boto3 calls in a worker thread. Keep `AIProvider` only while the starter endpoints exist.
- **Consequences:** Model-driven orchestration can be tested offline with scripted turns. Two provider abstractions coexist temporarily. A future direct Anthropic adapter would implement `ModelClient`.

## ADR-006: Local JSON trace export with redaction

- **Status:** accepted (2026-10-19); refined by ADR-011
- **Context:** Investigations need a trace for debugging and evaluation. `project.yaml` sets `database: null` and prioritises low cost and offline development. Traces must never contain secrets.
- **Decision:** A `TraceRecorder` captures steps in memory. A `Redactor` removes access key identifiers, 12-digit account identifiers, email addresses, and bearer tokens. A `TraceSink` writes one JSON file per investigation to a configurable, git-ignored directory. Writing is disabled unless the directory is configured. Tests use an in-memory sink or a temporary directory.
- **Consequences:** No new infrastructure is needed. Traces are not queryable, and retention is manual. Pattern-based redaction is defence in depth; the primary control is that secrets never enter tool fixtures or prompts.

## ADR-007: In-memory lexical runbook retrieval satisfies `rag` and `vector_database` for the MVP

- **Status:** accepted (2026-10-19)
- **Context:** `project.yaml` enables `rag`, `vector_database`, and `document_processing`, while the initial project brief excluded production vector infrastructure and the constraints favour low cost and offline work.
- **Decision:** Runbooks are synthetic Markdown fixtures, split into sections at load time (document processing) and searched by a deterministic lexical `RunbookRetriever` behind a protocol. No embedding model or vector database is used in the MVP.
- **Consequences:** Retrieval is deterministic and free, but has weaker recall than semantic search. A vector store can replace the retriever behind the same protocol later. The owner confirmed this interpretation of `vector_database: true` (D-1).

## ADR-008: Validate configuration at startup and map provider failures to safe errors

- **Status:** accepted (2026-10-19; implements NFR-8)
- **Context:** Misconfiguration previously surfaced only on the first request, `/health` always reported `ok`, and Bedrock SDK errors (missing credentials, access denied, unknown model, throttling) escaped as unhandled 500s whose messages can contain account or resource identifiers.
- **Decision:** `Settings` is a frozen Pydantic model built from the environment. Validation errors become a `ConfigurationError` listing each offending variable, never its value. The FastAPI lifespan validates settings and constructs the provider, so startup fails fast. `/health` repeats that check without network calls and returns 503 when misconfigured. The Bedrock adapter runs boto3 in a worker thread, builds its client from a fresh session with an explicit region, and translates SDK exceptions into `ProviderInvocationError` with a stable code. The API maps "unavailable" failures to 503 and rejected or malformed responses to 502. Only codes are logged.
- **Consequences:** Operators get immediate, actionable feedback without leaking values. Credentials cannot be verified without a network call, so `/health` reports readiness of configuration only. New Bedrock error codes default to `provider_request_failed` (502) until classified. No new dependency is added (`pydantic-settings` was not needed).

## ADR-009: Per-investigation tool session verifies evidence provenance

- **Status:** accepted (2026-10-19; implements FR-2 and FR-5 rule 1)
- **Context:** Reports must cite only evidence that a tool returned during the same investigation. A model-driven orchestrator (F-09) could otherwise invent or alter evidence that still passes schema validation. The registry is long-lived and must not hold per-request state.
- **Decision:** `ToolRegistry.session()` creates a `ToolSession` for each investigation. The orchestrator receives the session and may call tools only through it. The session records every `ToolResult` and the evidence returned. After assembling the report, `InvestigationService` rejects any evidence item that is not identical to one the session recorded (`UnverifiedEvidenceError`). Tools return raw output; the session validates it against the tool's typed output model, so schema failures become `malformed` results rather than exceptions.
- **Consequences:** Provenance is enforced in code for every orchestrator. The orchestrator protocol now takes the session (`investigate(request, tools)`). The session is the natural hook for trace recording in F-07. Fixture timestamps are filtered leniently so that unparsable records surface as `malformed` instead of disappearing.

## ADR-010: Deterministic findings rules

- **Status:** accepted (2026-10-19; implements FR-3, FR-4, FR-5, A-3 to A-6)
- **Context:** The deterministic orchestrator must produce explainable, reproducible findings that the evaluation harness (F-08) can score, and that a model-driven orchestrator (F-09) can be compared against. Every claim must cite evidence, and unsupported causes must not be presented as facts.
- **Decision:**
  - An abnormal signal is an alarm entering `ALARM` or an `ERROR` log event. Metric datapoints are context only, because no thresholds are defined. The onset is the earliest abnormal signal.
  - A change (deployment or infrastructure change) is a candidate cause when it starts at most 30 minutes before the onset. Each candidate becomes a hypothesis supported by the change and the abnormal signals at or after it.
  - Confidence: `low` when other evidence reports a different state for the change's entity; `high` when it is the only candidate, both an alarm and an error log followed it, and every change source answered; otherwise `medium`. Hypotheses are ordered by confidence, then by closeness to the onset.
  - Contradictions are detected through shared `entity` and `entity_state` evidence attributes (deployment records and structured deployment log fields).
  - Facts are restricted to direct restatements of evidence (onset, correlated changes, alarms, error-log counts). Remediation is proposed only for medium or high hypotheses, only when a safe reversal is known (a succeeded deployment with a previous version, or an infrastructure change), and always with `requires_human_approval: true`.
  - Runbook sections are recommended only when abnormal signals exist, phrased as "confirm its guidance applies before acting", because runbook text is untrusted.
- **Consequences:** Findings are explainable and testable but heuristic: causes outside recorded changes (dependencies, traffic) appear only as uncertainties and next steps, and lexical runbook matches can be imprecise. Changing a rule changes evaluation baselines, so rules are covered by scenario tests.

## ADR-011: Redact at ingestion and keep trace export non-fatal

- **Status:** accepted (2026-10-19; refines ADR-006 and implements FR-7, SEC-4)
- **Context:** ADR-006 planned redaction of exported traces only. SEC-4 also forbids secrets in reports, and tool output and free-text symptoms are where secrets would enter. A UUID whose last group is all digits also matched the naive 12-digit account-ID pattern.
- **Decision:** Redaction is applied where untrusted text enters: `ToolSession` redacts every evidence summary and attribute before it is recorded or returned, and `InvestigationService` redacts the symptoms (capped at the 2,000-character limit). Exported traces are redacted again as a whole. The account-ID pattern requires that the 12 digits are not adjacent to word characters or hyphens. Refused tool calls record argument names only. A trace that cannot be written is logged by investigation ID and error type, and the investigation still returns its report.
- **Consequences:** Reports, traces, and future model prompts share one redaction point per input. Redaction is pattern-based and can miss novel secret formats or over-redact unusual identifiers. Trace files can be missing for an investigation when the disk fails, which is preferred over failing the user's request.

## ADR-012: Scenario evaluation harness and instruction-like evidence

- **Status:** accepted (2026-10-19; implements FR-8, SEC-2, AC-10)
- **Context:** The deterministic orchestrator needs a reproducible baseline, and the model-driven orchestrator (F-09) must be scored the same way. The prompt-injection scenario showed that the runbook next step could point responders at a runbook section containing injected instructions.
- **Decision:**
  - Scenarios are data (`fixtures/evaluation_scenarios.json`, covered by the fixture hygiene test) with typed expectations: leading hypothesis category and change, required citations, sources that must not be used as support or recommendations, report and source statuses, and forbidden outcomes (`any_hypothesis`, `any_remediation`, `high_confidence`).
  - The runner lives in the package (`evaluation/`) so the same code scores deterministic, scripted, and live orchestrators. It uses the real `InvestigationService` with an in-memory trace and scores five checks; investigation failures are scored, not raised.
  - Citation validity is checked against the trace (source IDs returned by tool calls), independently of the service's own provenance check.
  - Evidence whose text resembles instructions (for example "ignore previous instructions", "call the … tool") is flagged with an uncertainty and excluded from recommended runbook steps. It stays in the report as data.
  - CI runs `python -m cloud_incident_service.evaluation`, which exits non-zero below `--min-pass-rate` (default 1.0).
- **Consequences:** Behaviour changes that alter findings show up as failing scenarios. The instruction detector is pattern-based and is a secondary control; the primary controls remain the read-only registry, delimited prompts (F-09), and report validation.
- **Revision (2026-10-19, approved by the owner):** `leading_hypothesis` and `leading_change` may list several equally correct values. The `multiple-changes` scenario accepts either correlated change (`deploy:orders-api:d-0107` or `change:orders-api:c-0503`) as the leading hypothesis, because both start within the correlation window and the earlier expectation encoded the deterministic tie-break rather than ground truth. It still requires citing both changes and forbids high confidence. `contradictory-records` stays strict.

## ADR-013: Model orchestrator owns evidence; the model writes analysis only

- **Status:** accepted (2026-10-19; implements ADR-002, ADR-005, D-2, D-7, A-7)
- **Context:** A model-driven orchestrator must not be able to invent evidence, hide failed sources, exceed cost bounds, or act on injected instructions, and it must be testable without a network.
- **Decision:**
  - `ModelClient.converse(system, messages, tools, max_output_tokens) -> ModelTurn` replaces the text-only `AIProvider`. `BedrockModelClient` maps it to the Converse API with tool use, temperature 0, and botocore connect/read timeouts and two retry attempts.
  - `ModelOrchestrator` runs a loop bounded by 8 tool calls (extra calls are refused with `limit_reached` and traced), 6 model turns, and a 60-second overall timeout. Tool calls go through the investigation's `ToolSession`, so unregistered tools and invalid arguments are refused and traced, and the refusal is returned to the model as an error result.
  - Tool results are sent as JSON with an explicit untrusted-data note; the system prompt (`prompts/investigation_v1.txt`, now `investigation_v4.txt`) forbids following instructions found in evidence.
  - The model's final answer is a JSON `ModelFindings` (summary, facts, hypotheses, uncertainties, next steps, remediation proposals) that cites evidence by `source_id`. The orchestrator supplies evidence, timeline, source statuses, report status, and source and instruction uncertainties from the tool results. Repeated calls to the same tool are merged per tool.
  - An answer that fails validation (malformed JSON, unknown citations, `requires_human_approval: false`, cut off at the token limit, or blocked by content filters) gets one repair message listing the validation errors without input values. A second failure raises `invalid_model_output` (HTTP 502); turn limits raise `model_turn_limit` (502) and timeouts `model_timeout` (504). Model turns (stop reason, requested tools, tokens, latency) and validation outcomes are traced.
  - `RuleBasedModelClient` stands in for a model when `AI_PROVIDER=mock`: it requests every tool, then answers with the deterministic findings derived from the tool payloads it received, so the full model path and the evaluation harness run offline.
- **Consequences:** Model output can only add analysis over verified evidence, and every failure mode is bounded and observable. The offline stand-in validates plumbing, not model quality; live quality is measured in F-10. Validation-driven repair costs at most one extra turn.
- **Revision (2026-10-19, after the first live run):** With Claude 3 Haiku, 6 of 10 scenarios failed validation even after repair, and the cause was not visible. Prompt `investigation_v2` states the answer rules explicitly (citable IDs only, no facts without evidence, empty findings when there is no evidence, lowercase enums, no extra keys) and the contradiction and confidence rules. Repair messages now list the citable source IDs, replace raw regex errors with actionable wording, and keep the one-repair limit. Capitalized `confidence` and `risk` values are lowercased before validation. Traces and evaluation failures include validation messages (without input values).

## ADR-014: Local-only, unauthenticated MVP API

- **Status:** accepted (2026-10-19; implements D-5, SEC-7)
- **Context:** `AGENTS.md` requires authorization boundaries to be validated, but the MVP is a single-user portfolio service that runs on a developer machine against synthetic data, with no deployment target (`deployment.provider: local`).
- **Decision:** The MVP API has no authentication or authorization. It is documented to run only on `127.0.0.1` (the uvicorn default), and `docs/SECURITY.md` lists authentication, authorization, TLS, edge rate and size limits, audit logging, and managed secrets as prerequisites for any non-local deployment. The remaining boundaries are enforced in code: input validation, the read-only tool registry, approval-gated remediation, and redaction.
- **Consequences:** No identity or tenancy code is written speculatively. Anyone who can reach the port can run investigations and spend Bedrock tokens when the model orchestrator is enabled, which is why the service must not be exposed. Adding authentication later is a contained change at the FastAPI boundary.

## ADR-015: Model hypotheses must follow the correlation rules

- **Status:** accepted (2026-10-19; implements A-3, A-5, A-6 and ADR-010 for the model path)
- **Context:** The second live run with Claude 3 Haiku passed 3 of 10 scenarios. Six failures had one root cause: the correlation rules that define a hypothesis were applied by the deterministic orchestrator and checked by the evaluation, but were neither stated precisely to the model nor enforced on its answers. Haiku blamed a deployment that started 70 minutes before the first signal, proposed causes and rollbacks when no change preceded the signals, rated one of two correlated changes high, and ignored a contradicted deployment record. The seventh failure was statements written as facts without evidence.
- **Decision:**
  - `orchestration/grounding.py` derives the correlation context from the tool evidence with the same rules as the deterministic path (onset, changes within 30 minutes before it, contradictions) and checks a model answer: every hypothesis cites a correlated change and no uncorrelated one; no hypotheses when nothing is correlated; nothing high when more than one change is correlated; a contradicted change must list the conflicting evidence and be rated low; no remediation for low hypotheses.
  - A violation is a validation failure handled by the existing single repair attempt. The repair message states the violations and the correlation facts (first abnormal signal and the candidate changes, or that there are none).
  - Hypotheses are ordered by confidence, then by closeness of the cited change to the onset, as in ADR-010.
  - A "fact" without evidence is moved to uncertainties before validation (FR-5 rule 2). The model still writes the summary, facts, hypothesis statements and rationales, next steps, and remediation wording.
  - Prompt `investigation_v3` states these rules, including the 30-minute window.
  - Revision after the third live run: prompt `investigation_v3` asked for next steps even when there was no evidence to cite, which the report rules forbid. `investigation_v4` requires next steps to cite evidence (none when there is no evidence), and a next step that cites nothing is moved to uncertainties as an unverified suggestion.
- **Consequences:** The model path and the deterministic path share one definition of a hypothesis, so evaluation results measure the model's evidence gathering, reading, and writing rather than its ability to infer unstated rules. The model can no longer propose causes outside recorded changes (for example a dependency outage); those belong in uncertainties and next steps, as on the deterministic path. Loosening that would be a requirements change.
