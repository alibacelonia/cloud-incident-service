# Changelog

All notable project changes are recorded here.

## Unreleased

- Generated from the `ai-service` profile, template version 0.2.0.
- Validate configuration at startup and report misconfiguration from `/health` with HTTP 503.
- Return stable error codes for Bedrock credential, access, throttling, connectivity, and request failures instead of HTTP 500.
- Run Bedrock calls off the event loop.
- Block network access in the default test suite.
- Add `POST /v1/investigations`, which validates incident requests (FR-1) and returns a schema-validated incident report. Reports are `partial` until evidence sources are added.
- Add incident domain models with FR-5 cross-reference validation.
- Remove the starter endpoints `/v1/generate`, `/v1/rag`, `/v1/documents`, and `/v1/documents/preview` and the in-memory document store (D-4). Runbook retrieval returns in F-05 behind `runbook_search`.
- Add the read-only tool registry and three fixture-backed tools (`cloudwatch_signals`, `deployment_history`, `infrastructure_changes`) with synthetic scenario data. Investigations now return evidence, an ordered timeline, and per-source status; unavailable or malformed sources make the report `partial`.
- Reject report evidence that no tool returned during the investigation.
- Add `runbook_search`: synthetic Markdown runbooks are split into sections with stable source IDs and ranked deterministically against the reported symptoms. Runbook text, including injected instructions, is returned as data only.
- Correlate evidence into facts, hypotheses with confidence and rationale, next steps, and approval-gated remediation proposals. Stale sources are flagged, contradictory records lower confidence and block remediation, and failed sources make reports `partial`.
- Add synthetic scenarios for multiple correlated changes, signals without a change, stale data, and contradictory records.
- Record a secret-free trace for every investigation and write it to `TRACE_DIR/<investigation_id>.json` when `TRACE_DIR` is set. `TRACE_DIR` is validated at startup and by `/health`.
- Redact secrets in tool output and reported symptoms before they reach reports or traces.
