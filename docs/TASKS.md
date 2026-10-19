# Tasks

Status values: `todo`, `in-progress`, `blocked`, `done`.

Priorities: **P0** is the MVP's deterministic core. **P1** completes the MVP (model-driven path,
Bedrock, documentation). **P2** is post-MVP. Requirement identifiers refer to `docs/PROJECT.md`.

**MVP status: complete (2026-10-19).** Features F-01 to F-11 are delivered and verified by the
owner. Remaining items are post-MVP (P2).

## Delivery workflow

Work is delivered one feature at a time:

1. Each feature is built on its own branch (`feature/f-NN-<slug>`) from `main`, with tests, and
   committed when lint and tests pass.
2. The feature ends with a sample test case the owner runs to confirm pass or fail.
3. After the owner confirms, the branch is merged into `main` and the next feature starts. No
   feature starts before the previous one is confirmed.

Each feature is a vertical slice that can be observed through the API or the test suite.

## Backlog

Release maintenance (2026-10-19): Python 3.12–3.14 CI matrix, locked installation,
scheduled dependency audit, and Dependabot configuration added for v0.1.0.
The HTTPX/Starlette TestClient deprecation warning is tracked as M-01: evaluate the
supported replacement, update test dependencies, and verify all API tests without the
warning before removal of upstream compatibility support. Status: `todo`, priority P2.

| ID | Priority | Status | Feature | Dependencies | Acceptance criteria |
| --- | --- | --- | --- | --- | --- |
| T-00 | P0 | done | Generate the starter application | none | Application and tests are present |
| T-01 | P0 | done | Refine product requirements | none | `docs/PROJECT.md` v1 approved 2026-10-19; D-1 to D-7 and A-1 to A-7 resolved; ADR-002 to ADR-007 accepted |
| F-01 | P0 | done | Configuration validation, readiness, and provider error handling | T-01 | AC-16 (NFR-8); Bedrock calls run off the event loop (NFR-4); a socket-blocking guard fails any default test that opens a network connection (NFR-1) |
| F-02 | P0 | done | Incident intake walking skeleton: domain models, `InvestigationService`, and `POST /v1/investigations` returning a valid `partial` report when no tools are registered | F-01 | AC-1, AC-2; unit tests for every FR-5 validation rule (uncited facts, unknown evidence identifiers, `requires_human_approval=false` rejected) |
| F-03 | P0 | done | Remove the starter endpoints (D-4) | F-02 | `/v1/generate`, `/v1/rag`, `/v1/documents`, and `/v1/documents/preview` return 404; their tests and README examples are removed |
| F-04 | P0 | done | Read-only tool registry, synthetic fixtures, and the three signal and change tools wired into the deterministic orchestrator | F-02, D-8 | AC-6, AC-8, AC-13; FR-5 rule 1 enforced (cited evidence must have been returned by a tool during the investigation); reports list evidence, timeline, and `data_sources`; A-3 look-back applied; repeated calls return identical output |
| F-05 | P0 | done | Runbook processing and the lexical `RunbookRetriever` behind `runbook_search` | F-04 | Runbook sections have stable `source_id`s; ranking is deterministic; injection text in runbooks is returned as data only |
| F-06 | P0 | done | Correlation and findings: facts, hypotheses, uncertainties, next steps, remediation proposals, and degraded-result handling | F-04, F-05 | AC-3, AC-4, AC-5, AC-7; A-3 correlation window, A-4 stale rule, and A-5 contradiction rule tested; all tools failing returns `partial` |
| F-07 | P0 | done | Investigation trace with redaction and JSON file export (D-3) | F-06 | AC-9; trace directory is configurable, git-ignored, and validated under NFR-8 |
| F-08 | P0 | done | Evaluation harness with the seven FR-8 scenarios | F-06, F-07 | AC-10; the runner reports schema validity, citation validity, hypothesis match, and safety per scenario |
| F-09 | P1 | done | `ModelClient` protocol, scripted offline client, Bedrock Converse tool-use adapter, and the bounded `ModelOrchestrator` | F-08 | A-7 limits enforced and tested; unregistered tool requests refused and traced; D-7 repair behaviour tested; prompt-injection scenario passes with scripted turns |
| F-10 | P1 | done | Opt-in live Bedrock tests and live evaluation | F-09, D-6 values | AC-12; runs bounded by time and tokens; results reported against D-6 thresholds |
| F-11 | P1 | done | Release documentation: security posture, Bedrock IAM policy example, CHANGELOG | F-10 | SEC-8 policy example; a new contributor can run an investigation from the README; AC-14, AC-15 |
| P-01 | P2 | todo | Design human approval before any side-effecting tool | MVP complete | A new ADR covers approval flow, audit trail, and authorization; no side-effecting tool exists until it is accepted |
| P-02 | P2 | todo | Real AWS read-only adapters behind the tool protocols | MVP complete | Adapters live outside the domain layer; least-privilege IAM documented; covered by opt-in live tests only |
| P-03 | P2 | todo | Direct Anthropic API `ModelClient` adapter | F-09 | Separate adapter with offline tests; live use is opt-in |

README usage is updated within each feature that changes how the service is run or called.

Verification (2026-10-19, F-11): `ruff format --check`, `ruff check`, `pytest` (316 passed, 3 live
deselected), and both evaluations (10/10) pass locally through `uv run`.

Live verification (2026-10-19, run by the owner with their account): Claude 3 Haiku
(`anthropic.claude-3-haiku-20240307-v1:0`, `ca-central-1`) passed all three live tests and 10/10
live evaluation scenarios, above the 80% threshold (D-6). Earlier live runs scored 0/10 (expired
SSO credentials), 1/10, 3/10, and 6/10; the fixes are recorded in ADR-013, ADR-015, and ADR-012.
One passing run is not a guarantee: model output varies between runs, so rerun the live suite
after prompt, model, or rule changes.
