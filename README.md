# cloud-incident-service

AI-assisted AWS incident investigation service

Generated from AI Project Factory's `ai-service` profile, version 0.2.0.

## Prerequisites and installation

- Python 3.12+ and uv

```bash
uv sync --extra dev
uv run uvicorn cloud_incident_service.main:app --reload
```

Open `http://localhost:8000/docs` or call:

```bash
curl http://localhost:8000/health
```

Start an incident investigation:

```bash
curl -X POST http://localhost:8000/v1/investigations \
  -H 'content-type: application/json' \
  -d '{"service":"payments-api","environment":"production","symptoms":"Elevated 5xx errors after 10:05 UTC","time_range":{"start":"2026-10-19T10:00:00Z","end":"2026-10-19T11:00:00Z"}}'
```

The response is a schema-validated incident report. Until evidence sources are added, every report
has `"status": "partial"` and an uncertainty stating that no sources were queried. Invalid input
(unknown environment, timestamps without a UTC offset, `end` not after `start`, a window over 24
hours, or unknown fields) returns HTTP 422.

The default `mock` provider is deterministic and needs no API key. Prompt templates live under `src/cloud_incident_service/prompts/` and are not used until model-driven investigations arrive. Selected external providers are initialized lazily and their credentials stay in environment variables.

## Configuration

| Variable | Values | Default |
| --- | --- | --- |
| `AI_PROVIDER` | `mock` or `bedrock` | `mock` |
| `AI_MODEL` | Bedrock model or inference profile ID; required for `bedrock` | empty |
| `AWS_REGION` | AWS region such as `us-east-1`; required for `bedrock` (`AWS_DEFAULT_REGION` is accepted as a fallback) | unset |

Settings are validated at startup. If any value is invalid, uvicorn exits with
`Application startup failed` and a message naming each offending variable (values are never
printed). `GET /health` returns 200 with `"status": "ok"` when settings are valid, or 503 with
`"status": "misconfigured"` and the same messages. It makes no network call, so AWS credentials
are only checked on the first model call.

Once investigations call a model, provider failures return a JSON body with a stable `code` and a safe `detail`:

| Status | Codes |
| --- | --- |
| 503 | `configuration_invalid`, `provider_credentials_unavailable`, `provider_credentials_invalid`, `provider_access_denied`, `provider_throttled`, `provider_unavailable`, `provider_unreachable` |
| 502 | `provider_request_rejected` (for example an unknown `AI_MODEL`), `provider_response_invalid`, `provider_request_failed` |

## Test and lint

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Tests inject a fake provider and never call a live model, AWS, or a vector database. A test guard fails any test that opens a network connection, and tests ignore your shell's AI and AWS settings.

## Requirements and Codex workflow

Edit `project.yaml` and `docs/PROJECT.md`. Ask Codex to use `$requirements-analysis`, review `docs/TASKS.md` and `docs/ARCHITECTURE.md`, then use `$feature-development`, `$testing`, and `$code-review`. Delegate architecture or security analysis to `software-architect` or `security-reviewer`; those agents inherit the parent sandbox and approvals.

## Template updates

Keep `.copier-answers.yml`. Back up or commit application work before applying a versioned template update, review the generated diff, resolve conflicts, and rerun validation. Automatic conflict-free upgrades are not promised.

