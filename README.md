# cloud-incident-service

An AI-assisted AWS incident investigation service. Given a short incident report, a single bounded
agent collects evidence from read-only tools, correlates it by time and service, and returns a
schema-validated report that keeps confirmed facts, hypotheses, uncertainty, and recommendations
separate. It never changes cloud resources: remediation proposals always require human approval.

This is a portfolio MVP that runs locally against synthetic data. It makes no claims about
production scale, reliability, or incident-resolution outcomes. Generated from AI Project
Factory's `ai-service` profile, version 0.2.0.

## Quick start

Prerequisites: Python 3.12+ and [uv](https://docs.astral.sh/uv/). No AWS account, API key, or
network access is needed.

```bash
uv sync --extra dev
uv run uvicorn cloud_incident_service.main:app --reload
```

The server listens on `127.0.0.1:8000`. Keep it there: the API has no authentication (see
[docs/SECURITY.md](docs/SECURITY.md)). Check that it is ready:

```bash
curl http://localhost:8000/health
```

Run your first investigation, a deployment that broke the payments API:

```bash
curl -s -X POST http://localhost:8000/v1/investigations \
  -H 'content-type: application/json' \
  -d '{"service":"payments-api","environment":"production","symptoms":"Elevated 5xx errors after 10:05 UTC","time_range":{"start":"2026-10-19T10:00:00Z","end":"2026-10-19T11:00:00Z"}}' \
  | python3 -m json.tool
```

The leading hypothesis is deployment `d-0042` (high confidence), with a rollback proposal that
requires approval. Interactive API docs are at `http://localhost:8000/docs`.

## What a report contains

| Field | Meaning |
| --- | --- |
| `status` | `complete`, or `partial` when a source was unavailable or malformed |
| `summary` | The first abnormal signal and the leading hypothesis |
| `evidence`, `timeline` | Evidence returned by tools, each with a stable `source_id`; the timeline lists events in order |
| `facts` | Statements that cite the evidence supporting them |
| `hypotheses` | Possible causes with supporting and contradicting evidence, confidence (`low`, `medium`, `high`), and a rationale |
| `uncertainties` | Missing, stale, contradictory, or suspicious data |
| `next_steps` | Diagnostic actions, each tied to evidence or a hypothesis |
| `remediation_proposals` | Suggested changes, always with `requires_human_approval: true` |
| `data_sources`, `provenance` | Each tool's status, and which orchestrator, model, and prompt version produced the report |

Requests need a `service` (lowercase letters, digits, hyphens), an `environment` (`production`,
`staging`, or `development`), `symptoms` (up to 2,000 characters), and a `time_range` of at most
24 hours with timezone-aware ISO-8601 timestamps. Invalid input returns HTTP 422.

## How it works

Four read-only tools return evidence from synthetic fixtures: `cloudwatch_signals` (alarms,
metrics, logs), `deployment_history` and `infrastructure_changes` (including the 60 minutes before
the window), and `runbook_search` (Markdown runbooks ranked against the symptoms). An abnormal
signal is an alarm entering `ALARM` or an error log; any change that started up to 30 minutes
before the first one becomes a hypothesis. Two orchestrators produce reports:

- `deterministic` (default): rule-based, reproducible findings ([ADR-010](docs/DECISIONS.md)).
- `model`: a model chooses the tools and writes the analysis within fixed limits (8 tool calls,
  6 turns, 60 seconds). It cites evidence by ID only; the service supplies the evidence itself, so
  the model cannot invent or alter it. Invalid answers get one repair attempt
  ([ADR-013](docs/DECISIONS.md)).

With the default `AI_PROVIDER=mock`, an offline rule-based stand-in plays the model, so the model
path also runs without a network:

```bash
ORCHESTRATOR=model uv run uvicorn cloud_incident_service.main:app
```

### Sample scenarios

All fixtures are fictional and dated 2026-10-19 (UTC). Use `"environment": "production"`.

| Service | Window | What it shows |
| --- | --- | --- |
| `payments-api` | 10:00–11:00 | Deployment followed by a 5xx alarm and error logs |
| `checkout-api` | 14:00–15:00 | Security group change in the look-back, then database errors |
| `orders-api` | 10:00–11:00 | Two correlated changes, so two medium-confidence hypotheses |
| `cart-api` | 12:00–13:00 | Abnormal signals with no correlated change |
| `billing-api` | 10:00–11:00 | `cloudwatch_signals` data is stale |
| `auth-api` | 15:00–16:00 | A deployment record contradicted by a log, so low confidence and no remediation |
| `inventory-api` | 16:00–17:00 | `deployment_history` unavailable, so the report is `partial` |
| `ledger-api` | 18:00–19:00 | A malformed deployment record is excluded, so the report is `partial` |
| `notify-api` | 10:00–11:00 | Prompt-injection text in a log line and a runbook, flagged and treated as data |
| `search-api` | any | No data from any source |

## Configuration

Copy `.env.example` for reference; settings are read from environment variables.

| Variable | Values | Default |
| --- | --- | --- |
| `AI_PROVIDER` | `mock` or `bedrock` | `mock` |
| `AI_MODEL` | Bedrock model or inference profile ID; required for `bedrock` | empty |
| `AWS_REGION` | AWS region such as `us-east-1`; required for `bedrock` (`AWS_DEFAULT_REGION` is accepted as a fallback) | unset |
| `ORCHESTRATOR` | `deterministic` or `model` | `deterministic` |
| `MODEL_MAX_OUTPUT_TOKENS` | Output-token cap per model turn, 256–8192 | `2048` |
| `TRACE_DIR` | Directory for investigation trace files, for example `.traces` (git-ignored); created at startup if missing | unset (no trace files) |
| `RUN_LIVE_TESTS` | `1` to enable live Bedrock tests | unset |

Settings are validated at startup. If any value is invalid, uvicorn exits with
`Application startup failed` and a message naming each offending variable (values are never
printed). `GET /health` returns 200 with `"status": "ok"`, or 503 with `"status": "misconfigured"`
and the same messages. It makes no network call, so AWS credentials are checked on the first model
call.

Failures return a JSON body with a stable `code` and a safe `detail`:

| Status | Codes |
| --- | --- |
| 503 | `configuration_invalid`, `provider_credentials_unavailable`, `provider_credentials_invalid`, `provider_access_denied`, `provider_throttled`, `provider_unavailable`, `provider_unreachable` |
| 502 | `provider_request_rejected` (for example an unknown `AI_MODEL`), `provider_response_invalid`, `provider_request_failed`, `invalid_model_output`, `model_turn_limit` |
| 504 | `model_timeout` |

### Using Amazon Bedrock

Set `AI_PROVIDER=bedrock`, `AI_MODEL`, and `AWS_REGION`, and provide credentials through the
default AWS credential chain (for example `AWS_PROFILE`). Use an account you are authorized to use,
enable model access for the model, and grant only the permissions in
[docs/iam/](docs/iam/) (`bedrock:InvokeModel` on that model).

## Investigation traces

When `TRACE_DIR` is set, every investigation writes `<investigation_id>.json` there: the request,
each tool call (validated arguments, status, returned source IDs, duration), refused calls
(argument names only), model turns (stop reason, requested tools, tokens, latency), validation
outcomes, and the final outcome. Traces, tool output, and reported symptoms are redacted for AWS
access key IDs, account IDs, email addresses, bearer tokens, and secret-looking values. A trace
that cannot be written is logged and never fails the investigation.

```bash
TRACE_DIR=.traces uv run uvicorn cloud_incident_service.main:app
```

## Test, lint, and evaluate

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run python -m cloud_incident_service.evaluation
uv run python -m cloud_incident_service.evaluation --orchestrator model
```

The default test suite is deterministic: a guard fails any test that opens a network connection,
and tests ignore your shell's AI and AWS settings. CI runs all five commands.

The evaluation runs ten scenarios from `src/cloud_incident_service/fixtures/evaluation_scenarios.json`
(the seven required cases plus multiple changes, an unavailable source, and a malformed source).
Each is scored on `schema`, `citations` (every cited source was returned by a tool, required
sources are cited, untrusted sources are never used as support or recommendations), `hypothesis`,
`sources`, and `safety` (no unregistered tool requests, every remediation requires approval). Add
`--json` for the full report or `--min-pass-rate 0.8` to change the exit threshold (default 1.0).

### Live Amazon Bedrock tests (opt-in)

Live tests are deselected by default. They only invoke the configured model, cap output tokens,
and are safe to rerun, but they incur Bedrock charges:

```bash
RUN_LIVE_TESTS=1 AI_MODEL=<model-or-inference-profile-id> AWS_REGION=<region> uv run pytest -m live -s
```

Without `RUN_LIVE_TESTS=1`, a model ID, a region, or credentials, each live test is skipped with
the reason. The suite runs a Converse smoke test, one model-driven investigation, and the full
evaluation, which must pass at least 80% of scenarios (D-6). The CLI equivalent is:

```bash
AI_PROVIDER=bedrock AI_MODEL=<model-id> AWS_REGION=<region> \
  uv run python -m cloud_incident_service.evaluation --orchestrator model --min-pass-rate 0.8
```

## Project documents

| Document | Contents |
| --- | --- |
| [docs/PROJECT.md](docs/PROJECT.md) | Requirements, acceptance criteria, and decisions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, interfaces, data flow, and error handling |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Architecture decision records |
| [docs/SECURITY.md](docs/SECURITY.md) | Security posture, controls, and limitations |
| [docs/TASKS.md](docs/TASKS.md) | Delivery backlog and status |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Notable changes |

## Contributing workflow

Requirements live in `project.yaml` and `docs/PROJECT.md`. Features are delivered one at a time
on `feature/f-NN-<slug>` branches, each with tests and documentation, and merged after review. The
repository skills (`.agents/skills/`) cover requirements analysis, feature development, testing,
and code review; `software-architect` and `security-reviewer` agents are available for
architecture and security analysis.

## License

[MIT](LICENSE)

## Template updates

Keep `.copier-answers.yml`. Back up or commit application work before applying a versioned template
update, review the generated diff, resolve conflicts, and rerun validation. Automatic
conflict-free upgrades are not promised.
