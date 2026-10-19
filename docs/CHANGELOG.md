# Changelog

All notable project changes are recorded here.

## Unreleased

- Generated from the `ai-service` profile, template version 0.2.0.
- Validate configuration at startup and report misconfiguration from `/health` with HTTP 503.
- Return stable error codes for Bedrock credential, access, throttling, connectivity, and request failures instead of HTTP 500.
- Run Bedrock calls off the event loop.
- Block network access in the default test suite.
