---
name: project-bootstrap
description: Initialize and validate a newly generated project when starting implementation from project.yaml and docs/PROJECT.md.
---

# Project bootstrap

1. Read `project.yaml`, `docs/PROJECT.md`, and applicable `AGENTS.md` files.
2. Inspect the repository before proposing new structure or dependencies.
3. Validate that the selected stack, enabled features, and local commands agree.
4. Identify the minimum required application components and any requirements conflicts.
5. Prepare a dependency-aware implementation plan with testable acceptance criteria.
6. Update `docs/ARCHITECTURE.md` and `docs/TASKS.md` only when the requirements support those changes.
7. Report assumptions and unresolved questions; do not silently resolve conflicting requirements.

If requirements explicitly add a graphical frontend with an enabled design section, route its visual architecture through the `ui-design` workflow when that skill exists. Otherwise keep this backend service free of UI design work and dependencies.

