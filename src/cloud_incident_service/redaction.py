"""Pattern-based redaction applied to tool output and traces (SEC-4, ADR-006).

This is defence in depth: the primary control is that secrets never enter fixtures, prompts,
or settings that are recorded.
"""

from __future__ import annotations

import re
from typing import Any

from cloud_incident_service.domain import Evidence

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[A-Z0-9]{16}\b")),
    (
        "aws_secret_access_key",
        re.compile(r"(?i)\baws_secret_access_key\b\s*[:=]\s*[A-Za-z0-9/+=]{16,}"),
    ),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}=*")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Not adjacent to word characters or hyphens, so UUID groups and longer IDs are kept.
    ("aws_account_id", re.compile(r"(?<![\w-])\d{12}(?![\w-])")),
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|authorization|api[_-]?key|credential)"
)


def redact_text(text: str) -> str:
    for kind, pattern in _PATTERNS:
        text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text


def redact(value: Any) -> Any:
    """Redact strings anywhere in a JSON-compatible structure."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        # Only string values under secret-looking keys are replaced; counts such as
        # `input_tokens` stay intact.
        return {
            redact_text(str(key)): "[REDACTED:secret]"
            if _SENSITIVE_KEY.search(str(key)) and isinstance(item, str) and item
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def redact_evidence(item: Evidence) -> Evidence:
    summary = redact_text(item.summary)
    attributes = redact(item.attributes)
    if summary == item.summary and attributes == item.attributes:
        return item
    return item.model_copy(update={"summary": summary, "attributes": attributes})
