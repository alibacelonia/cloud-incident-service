---
name: feature-development
description: Implement a requested feature in an existing project after its requirements and acceptance criteria are understood.
---

# Feature development

1. Inspect relevant code, tests, `project.yaml`, and requirements documentation.
2. Confirm the smallest complete change that satisfies the acceptance criteria.
3. Follow existing architecture and dependency patterns; avoid unrelated refactors.
4. Implement explicit validation and error handling at system boundaries.
5. Add focused tests, including failure cases that protect the new behavior.
6. Run relevant format, lint, and test commands and report their actual results.
7. Update `docs/TASKS.md`, and update architecture or decisions only when they changed.

Only route through a `ui-design` skill when this feature explicitly adds or changes a graphical frontend. API behavior and generated documentation alone are not graphical design-system changes.

