# Changelog

All notable project changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Opt-in live CloudWatch Logs source (`LOG_SOURCE=cloudwatch`) behind `cloudwatch_signals`: an
  allowlist of log groups, `logs:FilterLogEvents` only, capped pages, events, bytes, and time,
  sanitized evidence with hashed IDs, structured-`level` severity, a new `truncated` status, and a
  per-source `origin` that discloses mixing live and synthetic data (ADR-016). Live evidence is
  blocked from real models unless `ALLOW_LIVE_EVIDENCE_TO_MODEL=1`. Verified offline with a fake
  client; an opt-in live read test is included.
- Direct Anthropic API model provider: `AI_PROVIDER=anthropic` with `AI_MODEL` and
  `ANTHROPIC_API_KEY`, reusing the model orchestrator, validation, and stable error codes
  (ADR-018). Live tests follow `AI_PROVIDER`.
- Optional API-key authentication: set `SERVICE_API_KEY` to require an `X-API-Key` header on `/v1/`
  requests (401 otherwise), and a 32 KiB request-body limit (413) (SEC-6, SEC-7, ADR-017).

### Changed

- Test dependencies use `httpx2`, the HTTP backend Starlette's `TestClient` now supports, instead
  of `httpx`; this removes the `StarletteDeprecationWarning`, and any Starlette deprecation now
  fails the test suite.

## [0.1.0] - 2026-10-19

First release of the incident investigation service, built from the `ai-service` profile
(template version 0.2.0).

### Added

- CI coverage for Python 3.12, 3.13, and 3.14; a weekly dependency vulnerability audit;
  Dependabot updates for uv dependencies and GitHub Actions.

- `POST /v1/investigations`: validates an incident request (service, environment, symptoms,
  time range of at most 24 hours) and returns a schema-validated report with evidence, an event
  timeline, facts, hypotheses with confidence and rationale, uncertainties, next steps,
  approval-gated remediation proposals, per-source status, and provenance.
- Four read-only, fixture-backed tools behind a registry that rejects anything not declared
  read-only: `cloudwatch_signals`, `deployment_history`, `infrastructure_changes` (both with a
  60-minute look-back), and `runbook_search` over synthetic Markdown runbooks.
- Deterministic orchestrator: correlates changes that start up to 30 minutes before the first
  abnormal signal, flags stale and contradictory data, and handles empty, unavailable, and
  malformed sources (`partial` reports).
- Model-driven orchestrator (`ORCHESTRATOR=model`): a bounded tool-use loop (8 tool calls, 6
  turns, 60 seconds, configurable output-token cap) in which the model cites evidence by ID only,
  with one repair attempt for invalid answers. Amazon Bedrock Converse adapter with timeouts, and
  an offline rule-based stand-in for `AI_PROVIDER=mock`.
- Investigation traces written to `TRACE_DIR/<investigation_id>.json`: tool calls, refused calls,
  model turns, and validation outcomes.
- Evaluation harness: ten synthetic scenarios scored on schema, citations, hypothesis, sources, and
  safety (`python -m cloud_incident_service.evaluation`), required to pass 100% offline for both
  orchestrators in CI.
- Opt-in live Bedrock tests (`RUN_LIVE_TESTS=1 pytest -m live`) with an 80% live evaluation
  threshold.
- Startup and `/health` configuration checks with stable, value-free error messages; stable error
  codes for provider and model-loop failures.
- Documentation: requirements, architecture, ADR-001 to ADR-014, security posture, and
  least-privilege Bedrock IAM policy examples.

### Changed

- Model answers are checked against the same correlation rules as the deterministic path
  (ADR-015); prompt `investigation_v4` states them. Repair messages list the citable source IDs,
  the first abnormal signal, and the candidate changes, and explain format errors. Facts and next
  steps without evidence become uncertainties. Evaluation failures and traces include validation
  messages.
- Evaluation scenarios may accept several leading hypotheses; `multiple-changes` accepts either
  correlated change as leading.

### Removed

- The generated starter endpoints `/v1/generate`, `/v1/rag`, `/v1/documents`, and
  `/v1/documents/preview`, including the unauthenticated document upload and the open prompt
  pass-through.

### Security

- Secrets are redacted from tool output, reported symptoms, and traces; logs carry codes and
  identifiers only.
- Tool output is sent to the model as untrusted data, unregistered tool calls are refused and
  traced, and evidence that resembles injected instructions is flagged and never recommended.
- Reports may cite only evidence that a tool returned during the same investigation.
- The default test suite blocks network access and ignores the developer's AI and AWS settings;
  a hygiene test keeps fixtures free of real identifiers.
- The API has no authentication and is documented as local-only (`docs/SECURITY.md`).
