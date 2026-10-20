# v0.2 proposal: one real CloudWatch read adapter

Status: scope and requirements **approved by the owner on 2026-10-20**; the offline adapter is
implemented (F-14). Approval covers offline implementation only. It does not authorize AWS access,
provisioning, IAM changes, live reads, live model calls, or publication of real data: those need
the separate approvals below.
The approved v0.1 requirements and `project.yaml` remain authoritative.

## Intended outcome

Investigate an authorized sandbox service using a real CloudWatch log reader behind the
existing `cloudwatch_signals` tool contract. Begin with log events only; metrics, alarm
history, deployment sources, infrastructure changes, and cross-account access are deferred.
Fixture adapters remain the default and offline evaluation remains mandatory.

## Proposed requirements and acceptance tests

| ID | Requirement | Observable acceptance criterion |
| --- | --- | --- |
| V2-01 | Select the AWS adapter explicitly through validated configuration. | With no opt-in, clients are never constructed; the existing offline suite and both evaluations pass. Invalid configuration fails without revealing values. |
| V2-02 | Map service/environment to an operator-configured allowlist of log groups in one account and region. | Unknown mappings fail before SDK access; model arguments cannot select another group, account, or region. No resource discovery is performed. |
| V2-03 | Use only the approved read API, proposed `logs:FilterLogEvents`. | Fake-client tests assert the exact operation, log group, UTC bounds, and pagination arguments. No write API or Logs Insights query is available. |
| V2-04 | Bound collection. Proposed limits: 3 pages, 100 events, 64 KiB sanitized content, and a 4-second adapter deadline within the existing 5-second tool timeout. | Tests cover each cap, repeated continuation tokens, empty pages, throttling, timeout, and access denial; capped collection explicitly reports incomplete evidence. SDK retry/socket bounds fit the adapter deadline. |
| V2-05 | Normalize events into existing evidence models. | Stable IDs derive from source event identity without exposing account IDs or raw resource names; timestamps normalize to UTC; foreign service/environment evidence is rejected. Unstructured events are context only unless the approved severity parser identifies an error. |
| V2-06 | Sanitize before reports, model input, traces, or logging. | Canary tests cover credentials, account IDs, URLs, email, and arbitrary log text. Raw SDK exceptions and resource names never escape. Real data is never committed. Pattern redaction limitations remain documented. |
| V2-07 | Represent collection failures and incomplete data honestly. | Unavailable, malformed, stale, and truncated responses have explicit statuses/uncertainties. An accepted contract change defines truncation before code is written. |
| V2-08 | Keep live AWS evidence out of Bedrock by default. | AWS collection plus model mode fails closed unless the owner separately authorizes sending sanitized live evidence to the configured model. Fixture/live source mixing is visibly disclosed. |
| V2-09 | Prove adapter behavior offline first. | Botocore stubs/fakes cover pagination, filtering, normalization, privacy, caps, and all failure classes; socket-denial tests still pass. |
| V2-10 | Gate live verification separately. | Explicit approval records account authorization, region, exact groups, window, request cap, costs, retention, and whether a model is allowed. Without that approval and opt-in, tests skip. |

## Approval checkpoints

1. **Before implementation modifications:** approve this scope, API, limits, source mixing,
   severity parsing, truncation representation, and the configuration/contract proposal.
2. **Before AWS or IAM modifications:** approve exact targets and policies. No provisioning,
   role/policy attachment, test-data insertion, cleanup, deployment, or remediation is implied.
3. **Before live reads or paid evaluations:** approve a concrete run plan and maximum
   requests/tokens/cost. Use only an authorized sandbox with synthetic service logs.
4. **Before each later modifying or destructive step:** ask the owner explicitly, as requested
   on 2026-10-19. Preserve small commits and a reviewed rollback path. Never use destructive Git
   rollback or overwrite unrelated changes.

## Approved decisions (2026-10-20)

1. Logs only first; metrics, alarms, and deployment sources stay synthetic.
2. One account and region, an operator allowlist of log groups, and `FilterLogEvents` as the only
   API.
3. Limits: 3 pages, 100 events, 64 KiB of raw log text read, a 4-second adapter deadline, one SDK
   attempt with 1 s connect and 2 s read timeouts. Sanitized text is further limited to 300
   characters per event.
4. Incomplete collection is a new `truncated` source status; the report becomes `partial` with an
   uncertainty.
5. Only structured JSON logs with a `level` field establish abnormal signals; free text is context.
6. Other tools stay synthetic, and reports mark each source `fixture` or `live` and disclose mixing.
7. Live evidence is not sent to a real model unless `ALLOW_LIVE_EVIDENCE_TO_MODEL=1`; startup fails
   closed otherwise.

## Decisions that were required (resolved above)

- Approve logs-only first, or include metrics/alarms (which expands IAM and scope).
- Approve one-account/region allowlist configuration and `FilterLogEvents` as the sole API.
- Approve or adjust proposed caps and retry/deadline behavior.
- Decide how incomplete collection is represented in `ToolResult` and the report.
- Define a severity parser for the sandbox's structured logs; arbitrary text cannot reliably
  establish an abnormal signal under the current correlation rules.
- Decide whether other tools stay visibly synthetic during a live-log investigation.
- Approve the data policy before any live evidence can enter Bedrock or a trace file.

## Live model reliability study (separate future approval)

Proposed study: five runs of all ten existing synthetic scenarios using one model, region,
prompt version, and commit. Record pass rate per run/scenario, failures, latency, input/output
tokens, and estimated cost using verified current pricing. Preserve the existing 80% live
threshold; report variability without changing expectations to fit results. Agree a dollar
budget and token/request ceiling before executing. No repeated live runs are authorized now.

## Delivery order

Approve requirements/ADR -> approve contract/configuration change -> implement offline adapter
and tests in a feature branch -> verify/review -> owner acceptance -> separately approve live
sandbox reads -> separately approve model study -> decide release readiness.

## Reference points to verify during detailed design

- AWS `FilterLogEvents`: https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html
- CloudWatch Logs IAM: https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazoncloudwatchlogs.html

These are design reference links, not a claim that permissions or costs have been verified for
an owner's account. Detailed design must check current AWS documentation before implementation.
