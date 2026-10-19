---
name: testing
description: Design, add, or run tests for project behavior, regressions, edge cases, and external integration boundaries.
---

# Testing

Choose the narrowest layer that proves the behavior: unit tests for isolated logic, integration tests for boundaries, and smoke tests for startup and primary commands. Include error handling, edge cases, and a regression test for every reproduced bug.

Mock network, cloud, payment, email, database, and LLM boundaries unless the requirements explicitly call for an integration environment. Keep tests deterministic and free of real credentials or paid services. Run the relevant suite and distinguish passed, failed, and unexecuted checks in the report.

