# Security posture

cloud-incident-service is a portfolio MVP. It is designed to run locally against synthetic
data, investigate incidents with read-only tools, and keep every decision with a human. This
document describes what the MVP protects against, how, and what it deliberately does not do.

## Deployment posture (D-5)

- Authentication is **optional**: set `SERVICE_API_KEY` to require an `X-API-Key` header on every
  `/v1/` request (ADR-017). Without it the API is unauthenticated: run it only on your own machine,
  bound to `127.0.0.1` (the uvicorn default), never with `--host 0.0.0.0` or behind a public
  proxy. Startup logs a warning when no key is set.
- A single shared key is not authorization. Before any non-local deployment, also add TLS, per-user
  identity and authorization, request rate limits at the edge, audit logging, and managed secrets.
  Only the shared key and a 32 KiB request-body limit exist today.
- All tool data is synthetic (`src/cloud_incident_service/fixtures/`). Never load real customer
  logs, confidential runbooks, or data from a client or employer AWS account.

## Controls

| Requirement | Control | Where it is enforced and tested |
| --- | --- | --- |
| SEC-1 Read-only | The tool registry accepts only tools declared `read_only = True`; there are four read tools (fixture-backed by default) and no write path. The only AWS APIs the service calls are Bedrock model invocation, only when `AI_PROVIDER=bedrock`, and `logs:FilterLogEvents`, only when `LOG_SOURCE=cloudwatch` (no write, discovery, or Logs Insights calls); with `AI_PROVIDER=anthropic` the only outbound call is the Anthropic Messages API. | `tools/registry.py`; `tests/test_tool_registry.py` |
| SEC-2 Untrusted data | Tool output is sent to the model as JSON with an explicit untrusted-data note, the system prompt forbids following embedded instructions, unregistered tool calls are refused and traced, the model cites evidence by ID only, and evidence that resembles instructions is flagged and never recommended. | `orchestration/model.py`, `prompts/investigation_v4.txt`, `orchestration/findings.py`; prompt-injection evaluation scenario, `tests/test_model_orchestrator.py` |
| SEC-3 Human control | Remediation proposals are data with `requires_human_approval` fixed to `true`; no endpoint or tool executes them. | `domain/report.py`; `tests/test_domain_report.py`, evaluation safety check |
| SEC-4 Secrets | Credentials come only from the environment (`ANTHROPIC_API_KEY`, `SERVICE_API_KEY`) or the AWS default credential chain; provider keys are held as secrets and never logged, traced, or echoed, and `sk-ant-` keys are redacted from evidence and traces. Tool output, reported symptoms, and exported traces are redacted for AWS access key IDs, 12-digit account IDs, email addresses, bearer tokens, and string values under secret-looking keys. Logs record codes and identifiers, not payloads. | `redaction.py`, `tools/registry.py`, `application/investigation.py`; canary test in `tests/test_tracing.py` |
| SEC-5 Synthetic data | A hygiene test fails if fixtures contain 12-digit numbers, access key patterns, or email and URL domains outside reserved example domains. | `tests/test_fixture_hygiene.py` |
| SEC-6 Input bounds | Every request field has a type, pattern, or length limit; unknown fields are rejected; the time window is at most 24 hours. | `domain/request.py`; `tests/test_domain_request.py` |
| SEC-7 Access control | Optional shared API key checked in constant time before the body is read; keys are never logged or traced, and key settings are validated without echoing the value. | `api_security.py`, `config.py`; `tests/test_api_security.py` |
| SEC-8 Least privilege | IAM policy examples allow only `bedrock:InvokeModel` on the configured model and `logs:FilterLogEvents` on the allowlisted log groups. | `docs/iam/`; `tests/test_docs.py` |

## Model and cost bounds

A model-driven investigation is limited to 8 tool calls, 6 model turns, 60 seconds, and a
configurable output-token cap per turn (`MODEL_MAX_OUTPUT_TOKENS`, default 2048). Bedrock calls use
connect and read timeouts. Failures return stable error codes (`invalid_model_output`,
`model_turn_limit`, `model_timeout`, `provider_*`) without raw SDK messages, which can contain
account or resource identifiers.

## Data handling

- **Reports** contain the normalized request (with redacted symptoms) and redacted evidence.
- **Traces** are written only when `TRACE_DIR` is set, one JSON file per investigation. They hold
  tool arguments, statuses, evidence IDs, model token counts and latencies, and validation
  outcomes, but not evidence text or the arguments of refused calls. Retention is manual; keep the
  directory out of version control (`.traces/` is git-ignored).
- **Logs** contain investigation IDs, tool names, statuses, and error codes.

## Anthropic API usage

- `AI_PROVIDER=anthropic` sends the incident request, redacted tool evidence, and the system prompt
  to the Anthropic API. Use it only with synthetic data or data you may share with Anthropic, and
  review Anthropic's data-retention terms for your account first.
- Create a dedicated API key for this project, supply it only through `ANTHROPIC_API_KEY`, and
  revoke it in the Anthropic Console if it is ever exposed. Spend limits can be set there.
- Calls use connect and read timeouts, at most two retries, and the same call, turn, time, and
  output-token limits as Bedrock. Failures return the same stable `provider_*` codes.

## CloudWatch Logs usage

- `LOG_SOURCE=cloudwatch` is opt-in and reads only the log groups in `CLOUDWATCH_LOG_GROUPS`, one
  account and region. Model and API callers cannot select a log group, account, region, or query;
  unmapped services fail before any SDK call, and no resource discovery is performed.
- Collection is bounded (3 pages, 100 events, 64 KiB raw text, 4 seconds, one SDK attempt, 1 s
  connect and 2 s read timeouts). A capped read is reported as `truncated`, never as complete.
- Events are sanitized before they become evidence: control characters, URLs, access keys, account
  IDs, emails, bearer tokens, and secret-looking JSON keys are removed, and text is shortened.
  Evidence IDs are hashes, so log group names and event IDs are not exposed. Traces record source
  IDs and statuses, not event text. Redaction is pattern-based and can miss unusual secrets.
- Live evidence is blocked from real models unless `ALLOW_LIVE_EVIDENCE_TO_MODEL=1`; the service
  refuses to start with that combination otherwise. Reports mark each source `fixture` or `live`
  and disclose when they mix both.
- Use `docs/iam/cloudwatch-logs-filter-log-events-policy.json` (replace the placeholders). Read
  only synthetic logs from an account you are authorized to use; never point the service at
  production or customer logs.

## AWS usage

- Use an AWS account you are authorized to use for this project, and enable model access for the
  chosen model in the chosen region.
- Attach `docs/iam/bedrock-invoke-model-policy.json` for an on-demand foundation model, or
  `docs/iam/bedrock-invoke-inference-profile-policy.json` for an inference profile (cross-region
  profiles route to the foundation model in several regions, hence the region wildcard on that
  resource only). Replace the `<placeholders>` before use. `ConverseStream` is not used, so
  `bedrock:InvokeModelWithResponseStream` is not needed.
- Live tests are opt-in (`RUN_LIVE_TESTS=1`, `pytest -m live`), invoke the model only, and are
  safe to rerun; they still incur Bedrock charges.

## Known limitations

- Redaction and the instruction detector are pattern-based. They can miss novel secret formats or
  injection phrasing and can occasionally over-redact unusual identifiers; they are secondary
  controls behind the read-only registry and report validation.
- Authentication is a single optional shared key: no user identities, roles, key rotation without
  a restart, rate limiting, or multi-tenancy.
- Dependency auditing runs in CI and weekly using the locked dependency export. Dependabot
  proposes dependency and GitHub Actions updates; updates require review and passing checks.
  A clean audit only covers vulnerabilities known to the advisory service at scan time.
- Runbook retrieval is lexical and can surface loosely related sections; recommendations always ask
  the responder to confirm that guidance applies.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use the repository's private
vulnerability reporting, if it is enabled, or contact the maintainer privately.
