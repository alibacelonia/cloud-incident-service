# cloud-incident-service

> **Status:** MVP requirements v1, **approved by the owner on 2026-10-19**.
> Items tagged **[A-n]** follow a confirmed default and items tagged **[D-n]** follow a recorded
> decision; both are listed at the end of this document. This document and `project.yaml` are
> the authoritative requirements.
>
> **Implementation:** MVP complete (2026-10-19); all features F-01 to F-11 are delivered and
> verified by the owner; see `docs/TASKS.md`. Live
> Bedrock results depend on the owner's account and are not part of the default test suite.

## Project overview

Post-MVP planning: [v0.2 proposal](V0_2_PLAN.md) scopes one real CloudWatch log reader.
It is pending owner approval and does not change the approved MVP scope or authorize AWS access.

A local-first FastAPI service that investigates an AWS-style service incident with a single,
bounded, tool-using agent. It collects evidence from narrowly scoped read-only tools backed by
synthetic fixtures, correlates that evidence by timestamp and service, and returns a
schema-validated incident report that keeps confirmed facts, hypotheses, uncertainty, and
recommendations separate. It never changes cloud resources.

This is a public portfolio project. It demonstrates design and engineering practice. It makes no
claims about production scale, reliability, mean time to resolution, or cost outcomes.

## Problem statement

Cloud incident responders gather evidence by hand from metrics, alarms, logs, deployment records,
infrastructure changes, and runbooks, then correlate it mentally under time pressure. The service
reduces that collection and correlation overhead while keeping every claim traceable to its
evidence and every decision under human control.

## Target users

- **Primary:** an AWS-focused DevOps, Cloud Operations, or Site Reliability Engineer investigating
  a degraded service or failed deployment.
- **Secondary:** a maintainer or reviewer who inspects investigation traces and evaluation results
  to judge the agent's behaviour.

## Glossary

| Term | Meaning |
| --- | --- |
| Incident request | The user's input: service, environment, symptoms, and time range. |
| Tool | A typed, read-only function that returns evidence from one source. |
| Evidence | A normalized item returned by a tool, identified by a stable `source_id`. |
| Fact | A statement directly supported by one or more evidence items. |
| Hypothesis | A possible cause, with supporting and contradicting evidence and a confidence level. |
| Uncertainty | A known gap: missing, stale, contradictory, or unavailable data. |
| Next step | A recommended diagnostic action the responder performs. |
| Remediation proposal | A suggested change that is never executed and always requires human approval. |
| Trace | A secret-free record of an investigation's inputs, tool calls, model calls, and validation. |

## Functional requirements

### FR-1 Incident intake

`POST /v1/investigations` accepts a JSON body with:

| Field | Rule |
| --- | --- |
| `service` | 1–64 characters matching `^[a-z0-9][a-z0-9-]*$` [A-1] |
| `environment` | One of `production`, `staging`, `development` [A-1] |
| `symptoms` | Free text, 1–2,000 characters |
| `time_range.start`, `time_range.end` | ISO-8601 timestamps with an explicit offset; normalized to UTC |

- `end` must be later than `start`; the window must not exceed 24 hours [A-2].
- Unknown fields are rejected.
- Invalid input returns HTTP 422 with field-level errors, and no tool is executed.
- Investigations run synchronously within the request (`background_jobs: false`).

### FR-2 Read-only tools

The MVP provides exactly four tools. Each has a typed Pydantic input and output and is backed by
local synthetic fixtures.

| Tool | Returns |
| --- | --- |
| `cloudwatch_signals` | Metric datapoints, alarm state changes, and log events for the service and environment |
| `deployment_history` | Deployments: identifier, version, status, start and finish times |
| `infrastructure_changes` | Configuration and infrastructure change events, with the changed resource |
| `runbook_search` | Runbook sections relevant to the service and symptoms |

- Each tool result has a `status` of `ok`, `empty`, `unavailable`, or `malformed`; a `data_as_of`
  timestamp; and zero or more evidence items, each with a stable `source_id` such as
  `deploy:payments-api:d-0042`.
- Change tools (`deployment_history`, `infrastructure_changes`) query from 60 minutes before
  `start` until `end`; `cloudwatch_signals` queries the requested window only [A-3].
- The same tool input and fixtures always produce the same output.
- The tool registry accepts only tools declared read-only. Registering any other tool fails at
  startup. A request to invoke a tool name that is not registered is rejected, recorded in the
  trace, and never executed.

### FR-3 Degraded tool results

| Condition | Required behaviour |
| --- | --- |
| Empty | Recorded as "no data from `<tool>`" in uncertainties. No evidence is invented. |
| Unavailable (exception or timeout) | Investigation still completes; report `status` is `partial`; the source is listed as unavailable. HTTP 200. |
| Malformed (output fails schema validation) | Treated as unavailable for that source. Raw content is excluded from the report; the validation error is recorded in the trace. |
| Stale (`data_as_of` earlier than `time_range.end`) | Source flagged `stale` and an uncertainty is recorded [A-4]. |
| Contradictory | Both evidence items are kept and a contradiction is recorded in uncertainties. A hypothesis with any contradicting evidence cannot be rated `high` [A-5]. |

If every tool is unavailable or malformed, the report is returned with `status` `partial`, no facts,
no hypotheses, and an uncertainty explaining that no evidence could be collected.

### FR-4 Correlation

- Tool output is normalized to a common evidence shape: `source_id`, `tool`, `service`,
  `environment`, `timestamp` (UTC), `kind`, `summary`, and typed `attributes`.
- Evidence for other services or environments is excluded. Fixtures include such distractors.
- The timeline is ordered by `timestamp`, then `source_id`, so ordering is deterministic.
- A change event is temporally correlated with a signal when it precedes the signal's onset by
  30 minutes or less [A-3].

### FR-5 Structured incident report

The response body is an `IncidentReport` with:

- `investigation_id` (UUID), `status` (`complete` or `partial`), `generated_at`, and the
  normalized request;
- `summary` (at most 1,000 characters);
- `evidence`: every evidence item used, each with its `source_id`;
- `timeline`: ordered `source_id` references;
- `facts`: each with a `statement` and at least one `evidence_id`;
- `hypotheses`: each with a `statement`, at least one `supporting_evidence_id`, optional
  `contradicting_evidence_ids`, `confidence` (`low`, `medium`, `high`) [A-6], and a `rationale`;
- `uncertainties`: each with a `description` and, where applicable, the related tool or evidence;
- `next_steps`: diagnostic actions, each referencing the evidence or hypothesis that motivates it;
- `remediation_proposals`: each with an `action`, `rationale`, `hypothesis_id`, `risk`, and
  `requires_human_approval`, which is always `true`;
- `data_sources`: per-tool status (`ok`, `empty`, `stale`, `unavailable`, `malformed`);
- `provenance`: orchestrator type, model identifier or `deterministic`, and prompt version.

Validation rules, enforced by the schema for both deterministic and model-generated reports:

1. Every referenced evidence identifier exists in `evidence` and was returned by a tool during this
   investigation.
2. A fact without evidence is invalid. Unsupported statements must be hypotheses or uncertainties.
3. `requires_human_approval` cannot be `false`.
4. A model-generated report that fails validation is not returned as valid output [D-7].

### FR-6 Orchestration

- A single agent performs the investigation. No multi-agent orchestration.
- **Deterministic orchestrator** (default; used by the default test suite): calls each of the four
  tools once, applies the correlation rules, and builds the report from rule-based templates. For
  the same request and fixtures, the report is identical apart from `investigation_id` and
  `generated_at`.
- **Model-driven orchestrator** (opt-in) [D-2]: a bounded tool-use loop of at most 8 tool calls,
  6 model turns, a 60-second overall timeout, and a configured output-token cap [A-7]. Tool calls
  are restricted to the registry. The final output is validated against FR-5.
- Prompt templates live in versioned files; the prompt version is recorded in `provenance` and the
  trace.

### FR-7 Investigation trace

- Every investigation produces a trace containing: `investigation_id`, normalized request,
  orchestrator, model identifier, prompt version, ordered steps (tool name, validated input,
  status, returned `source_id`s, duration; model turn latency and token usage), validation
  outcomes, and final report status.
- The trace is exported as JSON [D-3].
- Before export, the trace is redacted for AWS access key identifiers, 12-digit account
  identifiers, email addresses, and bearer tokens. Credentials and environment variables are never
  recorded.

### FR-8 Evaluation harness

- At least seven synthetic scenarios: faulty deployment, infrastructure configuration change,
  signals with no correlated change, no data, stale data, contradictory records, and prompt
  injection embedded in a log line and a runbook.
- Each scenario defines the request, the expected leading hypothesis category (or none), the
  `source_id`s that must be cited, and forbidden outcomes.
- The runner reports per scenario: schema validity, citation validity, expected-hypothesis match,
  and safety (no unregistered tool call; every remediation proposal requires approval).
- The deterministic orchestrator must pass 100% of scenarios in the default test suite.
- Live model evaluation is opt-in; its pass thresholds are pending [D-6].

### FR-9 Starter endpoints

The generated `/v1/generate`, `/v1/rag`, `/v1/documents`, and `/v1/documents/preview` endpoints are
not part of the incident workflow. Their future is pending [D-4]. Until decided they remain
unchanged.

## Non-functional requirements

- **NFR-1 Offline tests:** the default `pytest` run makes no network, AWS, or model calls. A test
  guard fails any test that opens a network socket.
- **NFR-2 Low cost:** no paid service, account, or secret is needed to install, run, or test the
  default configuration.
- **NFR-3 Live tests:** marked `live`, deselected by default, and skipped with an explicit reason
  unless opted in and configured. They are bounded by timeout and token limits, perform only model
  invocation, and are safe to rerun.
- **NFR-4 Event loop safety:** blocking SDK calls (for example boto3) run off the event loop.
- **NFR-5 Logging:** logs carry `investigation_id`; prompts, tool payloads, and credentials are not
  logged by default.
- **NFR-6 Quality gates:** `ruff format --check`, `ruff check`, and `pytest` pass locally and in CI.
- **NFR-7 Reproducibility:** commands in the README install, run, and test the project on a clean
  machine with Python 3.12+ and uv.
- **NFR-8 Configuration errors:**
  1. All settings are validated by a typed settings model. Invalid settings stop application
     startup with an error that names each offending environment variable and never echoes its
     value.
  2. `GET /health` returns 200 with `status: ok` when settings are valid and the selected provider
     can be constructed. Otherwise it returns 503 with `status: misconfigured` and the same safe
     error messages. It makes no network call, so it cannot verify credentials.
  3. Provider runtime failures never surface as an unhandled 500. Missing or expired credentials,
     access denied, throttling, and an unreachable endpoint return 503. A rejected request (such as
     an unknown model ID) or an unexpected response shape returns 502. Each response carries a
     stable `code` and a safe `detail`, never the raw SDK message.
  4. Every case above has an offline test.

## Security and safety requirements

- **SEC-1 Read-only:** no MVP tool can create, modify, or delete cloud resources. The only AWS API
  the MVP may call is Bedrock model invocation, and only when explicitly selected.
- **SEC-2 Untrusted data:** tool output, logs, and runbooks are passed to the model as clearly
  delimited data, never as instructions. The prompt-injection scenario must produce no
  unregistered tool call and no fact lacking evidence.
- **SEC-3 Human control:** remediation proposals are inert data. No endpoint or tool executes them.
- **SEC-4 Secrets:** credentials come only from the environment or the AWS default credential chain.
  They never appear in request bodies, logs, traces, reports, or fixtures.
- **SEC-5 Synthetic data:** fixtures contain only fictional services and reserved values (for
  example `example.com`). A test fails if fixtures contain 12-digit numbers, access key patterns,
  or email addresses outside reserved example domains.
- **SEC-6 Input bounds:** all request fields have maximum lengths, as specified in FR-1.
- **SEC-7 Access control:** pending [D-5].
- **SEC-8 Least privilege:** documentation provides a Bedrock IAM policy example that is limited to
  invoking the configured model.

## MVP features

1. `POST /v1/investigations` returning a schema-validated `IncidentReport`.
2. Four deterministic, fixture-backed, read-only tools behind a read-only registry.
3. Correlation, including handling of empty, stale, contradictory, malformed, and unavailable data.
4. A deterministic orchestrator, plus an opt-in model-driven orchestrator using Amazon Bedrock [D-2].
5. Secret-free investigation traces exported as JSON.
6. A scenario-based evaluation harness.
7. Opt-in, bounded live Bedrock tests.

## Out of scope

- Production deployment and cloud resource provisioning.
- Optional features not enabled in `project.yaml` (OpenAI API, background jobs, a relational
  database, Docker).
- Autonomous remediation, side-effecting tools, and any production change.
- An approval workflow that executes actions. Proposals are recorded only.
- Multi-agent orchestration.
- Production vector infrastructure and hosted embedding services [D-1].
- Real AWS data sources (CloudWatch, deployment services, CloudTrail, AWS Config). These are a
  post-MVP candidate behind the same tool interfaces.
- Ingestion of real customer logs or confidential runbooks.
- Use of a client or employer AWS account without explicit authorization.
- A direct Anthropic API provider. This is a post-MVP candidate as a separate adapter.
- A graphical frontend.
- Claims of reduced mean time to resolution, production reliability, or cost savings without
  measured evidence.

## Acceptance criteria

| ID | Criterion | Requirements |
| --- | --- | --- |
| AC-1 | A valid request to `POST /v1/investigations` returns HTTP 200 with a body that validates against `IncidentReport`. | FR-1, FR-5 |
| AC-2 | Each invalid-input case (bad service, unknown environment, naive timestamp, end ≤ start, window > 24 h, oversized symptoms, unknown field) returns HTTP 422, and no tool is called. | FR-1, SEC-6 |
| AC-3 | Every fact and hypothesis in every scenario report references evidence returned during that investigation. A report that violates this fails validation. | FR-5 |
| AC-4 | Facts, hypotheses, uncertainties, next steps, and remediation proposals are separate fields, and every remediation proposal has `requires_human_approval: true`. | FR-5, SEC-3 |
| AC-5 | Empty, stale, contradictory, malformed, and unavailable tool results each have a test asserting the FR-3 behaviour. | FR-3 |
| AC-6 | Evidence for other services or environments is never included, and timeline order is deterministic. | FR-4 |
| AC-7 | Running the same scenario twice with the deterministic orchestrator yields identical reports apart from `investigation_id` and `generated_at`. | FR-6 |
| AC-8 | The registry rejects a tool not declared read-only, and an unregistered tool call is refused and recorded in the trace. | FR-2, SEC-1 |
| AC-9 | Every investigation produces a JSON trace. Planted canary secrets in tool output and the environment do not appear in it. | FR-7, SEC-4 |
| AC-10 | The deterministic orchestrator passes 100% of evaluation scenarios, including prompt injection. | FR-8, SEC-2 |
| AC-11 | The default test suite passes with networking blocked and no AWS or model configuration. | NFR-1, NFR-2 |
| AC-12 | Live Bedrock tests are deselected by default and skip with a clear reason when not opted in or configured. | NFR-3 |
| AC-13 | The fixture hygiene test passes. | SEC-5 |
| AC-14 | `ruff format --check`, `ruff check`, and `pytest` pass locally and in the generated CI workflow. | NFR-6 |
| AC-15 | Architecture and security tradeoffs are recorded in `docs/DECISIONS.md`. | — |
| AC-16 | An invalid setting stops startup with a message naming the variable; `/health` reports `misconfigured` with 503; each provider failure category in NFR-8 returns its documented status and `code` without exposing SDK messages. | NFR-8 |

## Confirmed defaults

Approved by the owner on 2026-10-19.

| ID | Default |
| --- | --- |
| A-1 | Service name pattern `^[a-z0-9][a-z0-9-]*$` (1–64 characters); environment enum `production`, `staging`, `development`. |
| A-2 | Time range required, timezone-aware, at most 24 hours. |
| A-3 | 60-minute look-back for change tools; correlation when a change precedes signal onset by 30 minutes or less. |
| A-4 | Stale means `data_as_of` is earlier than `time_range.end`. |
| A-5 | Contradiction means two evidence items about the same entity with incompatible states, or a hypothesis with both supporting and contradicting evidence; contradicted hypotheses are capped below `high`. |
| A-6 | Categorical confidence (`low`, `medium`, `high`) with a written rationale, not numeric scores. |
| A-7 | Model loop limits: 8 tool calls, 6 turns, 60 seconds, configurable output-token cap. |

## Decisions

| ID | Decision | Status |
| --- | --- | --- |
| D-1 | `rag` and `vector_database` are satisfied in the MVP by a local, in-memory lexical runbook retriever behind a `RunbookRetriever` protocol. No vector database or embedding service is used. | Approved 2026-10-19 |
| D-2 | The bounded model-driven tool-use loop is part of the MVP (P1), built after the deterministic path. | Approved 2026-10-19 |
| D-3 | Traces are written as one JSON file per investigation to a configurable, git-ignored local directory. Writing is off unless the directory is configured; tests use a temporary directory. | Approved 2026-10-19 |
| D-4 | The starter endpoints (`/v1/generate`, `/v1/rag`, `/v1/documents`, `/v1/documents/preview`) are removed once `/v1/investigations` lands. Runbooks come from repository fixtures. | Approved 2026-10-19 |
| D-5 | No authentication in the MVP. The service is documented as local-only (bind to `127.0.0.1`); authentication is a prerequisite for any non-local deployment. | Approved 2026-10-19 |
| D-6 | Live evaluation must pass at least 80% of scenarios. The owner runs live tests with their own authorized personal AWS account and supplies the Bedrock model ID and region through `AI_MODEL` and `AWS_REGION` at run time; neither is committed. Live evaluations report results and do not block CI. | Approved 2026-10-19 |
| D-7 | Invalid model output gets one repair attempt with the validation errors. If it is still invalid, the API returns HTTP 502 `invalid_model_output` and the failure is recorded in the trace. | Approved 2026-10-19 |
| D-8 | Tools keep AWS-specific names (`cloudwatch_signals`, `deployment_history`, `infrastructure_changes`, `runbook_search`) because the product is AWS-focused; no generic provider layer is added. | Approved 2026-10-19 |
