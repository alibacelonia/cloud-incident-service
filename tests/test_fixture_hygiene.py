"""SEC-5: fixtures must contain only synthetic, fictional data."""

import re
from importlib.resources import files
from pathlib import Path

import pytest

FIXTURE_DIR = Path(str(files("cloud_incident_service.fixtures")))
FIXTURE_FILES = sorted(
    path for path in FIXTURE_DIR.rglob("*") if path.is_file() and path.suffix != ".pyc"
)

TWELVE_DIGITS = re.compile(r"(?<!\d)\d{12}(?!\d)")
ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
RESERVED_DOMAIN = re.compile(r"(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.(example|invalid|test))$")


def test_fixture_files_exist() -> None:
    assert {path.name for path in FIXTURE_FILES} >= {
        "cloudwatch_signals.json",
        "deployment_history.json",
        "infrastructure_changes.json",
    }


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda path: path.name)
def test_fixture_contains_no_real_identifiers(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert not TWELVE_DIGITS.search(text), "possible AWS account ID"
    assert not ACCESS_KEY.search(text), "possible AWS access key ID"
    for domain in EMAIL.findall(text) + URL_HOST.findall(text):
        assert RESERVED_DOMAIN.search(domain.lower()), f"non-reserved domain: {domain}"


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("account 123456789012", TWELVE_DIGITS),
        ("key AKIAABCDEFGHIJKLMNOP", ACCESS_KEY),
    ],
)
def test_hygiene_patterns_detect_planted_values(text: str, pattern: re.Pattern) -> None:
    assert pattern.search(text)


@pytest.mark.parametrize(
    ("domain", "reserved"),
    [("example.com", True), ("ops.example.org", True), ("host.invalid", True), ("corp.io", False)],
)
def test_reserved_domain_rule(domain: str, reserved: bool) -> None:
    assert bool(RESERVED_DOMAIN.search(domain)) is reserved
