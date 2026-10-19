# Changelog

All notable project changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - Unreleased (MVP)

First release of the incident investigation service, built from the `ai-service` profile
(template version 0.2.0).

### Added

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

- Model prompt `investigation_v2` with explicit answer rules; repair messages list citable source
  IDs and explain format errors; evaluation failures and traces include validation messages.

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
