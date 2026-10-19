# Security posture

cloud-incident-service is a portfolio MVP. It is designed to run locally against synthetic
data, investigate incidents with read-only tools, and keep every decision with a human. This
document describes what the MVP protects against, how, and what it deliberately does not do.

## Deployment posture (D-5)

- The API has **no authentication or authorization**. Run it only on your own machine, bound to
  `127.0.0.1` (the uvicorn default). Do not start it with `--host 0.0.0.0` or put it behind a
  public proxy.
- Before any non-local deployment, add at least: authentication and authorization on every route,
  TLS, request rate and size limits at the edge, audit logging, and managed secrets. None of these
  exist in the MVP.
- All tool data is synthetic (`src/cloud_incident_service/fixtures/`). Never load real customer
  logs, confidential runbooks, or data from a client or employer AWS account.

## Controls

| Requirement | Control | Where it is enforced and tested |
| --- | --- | --- |
| SEC-1 Read-only | The tool registry accepts only tools declared `read_only = True`; there are four fixture-backed read tools and no write path. The only AWS API the service calls is Bedrock model invocation, and only when `AI_PROVIDER=bedrock`. | `tools/registry.py`; `tests/test_tool_registry.py` |
| SEC-2 Untrusted data | Tool output is sent to the model as JSON with an explicit untrusted-data note, the system prompt forbids following embedded instructions, unregistered tool calls are refused and traced, the model cites evidence by ID only, and evidence that resembles instructions is flagged and never recommended. | `orchestration/model.py`, `prompts/investigation_v4.txt`, `orchestration/findings.py`; prompt-injection evaluation scenario, `tests/test_model_orchestrator.py` |
| SEC-3 Human control | Remediation proposals are data with `requires_human_approval` fixed to `true`; no endpoint or tool executes them. | `domain/report.py`; `tests/test_domain_report.py`, evaluation safety check |
| SEC-4 Secrets | Credentials come only from the environment or the AWS default credential chain. Tool output, reported symptoms, and exported traces are redacted for AWS access key IDs, 12-digit account IDs, email addresses, bearer tokens, and string values under secret-looking keys. Logs record codes and identifiers, not payloads. | `redaction.py`, `tools/registry.py`, `application/investigation.py`; canary test in `tests/test_tracing.py` |
| SEC-5 Synthetic data | A hygiene test fails if fixtures contain 12-digit numbers, access key patterns, or email and URL domains outside reserved example domains. | `tests/test_fixture_hygiene.py` |
| SEC-6 Input bounds | Every request field has a type, pattern, or length limit; unknown fields are rejected; the time window is at most 24 hours. | `domain/request.py`; `tests/test_domain_request.py` |
| SEC-7 Access control | Not implemented; see the deployment posture above. | — |
| SEC-8 Least privilege | IAM policy examples allow only `bedrock:InvokeModel` on the configured model. | `docs/iam/`; `tests/test_docs.py` |

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
- There is no authentication, authorization, rate limiting, or multi-tenancy.
- Dependency vulnerability scanning is not configured in CI.
- Runbook retrieval is lexical and can surface loosely related sections; recommendations always ask
  the responder to confirm that guidance applies.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use the repository's private
vulnerability reporting, if it is enabled, or contact the maintainer privately.
