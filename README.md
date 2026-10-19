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
curl -X POST http://localhost:8000/v1/generate \
  -H 'content-type: application/json' \
  -d '{"input":"Summarize the local starter"}'
```

The default `mock` provider is deterministic and needs no API key. Prompt templates live under `src/cloud_incident_service/prompts/`. Selected external providers are initialized lazily and their credentials stay in environment variables.

## Test and lint

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Tests inject a fake provider and never call a live model, AWS, or a vector database.

## Requirements and Codex workflow

Edit `project.yaml` and `docs/PROJECT.md`. Ask Codex to use `$requirements-analysis`, review `docs/TASKS.md` and `docs/ARCHITECTURE.md`, then use `$feature-development`, `$testing`, and `$code-review`. Delegate architecture or security analysis to `software-architect` or `security-reviewer`; those agents inherit the parent sandbox and approvals.

## Template updates

Keep `.copier-answers.yml`. Back up or commit application work before applying a versioned template update, review the generated diff, resolve conflicts, and rerun validation. Automatic conflict-free upgrades are not promised.

