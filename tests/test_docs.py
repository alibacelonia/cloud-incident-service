"""Keep the release documentation consistent with the code (F-11)."""

import json
import re
from pathlib import Path

import pytest

from cloud_incident_service.config import ENVIRONMENT_VARIABLES
from cloud_incident_service.domain import IncidentRequest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
IAM_POLICIES = sorted((ROOT / "docs" / "iam").glob("*.json"))


def test_readme_investigation_example_is_a_valid_request() -> None:
    bodies = re.findall(r"-d '(\{.*?\})'", README)

    assert bodies, "README has no example request body"
    for body in bodies:
        IncidentRequest.model_validate_json(body)


@pytest.mark.parametrize("variable", sorted(ENVIRONMENT_VARIABLES.values()))
def test_every_setting_is_documented(variable: str) -> None:
    assert f"`{variable}`" in README
    assert f"{variable}=" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_readme_links_point_to_existing_files() -> None:
    links = re.findall(r"\]\(((?!https?://)[^)#]+)\)", README)

    assert links
    for link in links:
        assert (ROOT / link).exists(), link


def test_iam_policy_examples_exist() -> None:
    assert [path.name for path in IAM_POLICIES] == [
        "bedrock-invoke-inference-profile-policy.json",
        "bedrock-invoke-model-policy.json",
        "cloudwatch-logs-filter-log-events-policy.json",
    ]


def statements(path: Path) -> list[tuple[str, list[str], list[str]]]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    assert policy["Version"] == "2012-10-17"
    found = []
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"
        actions, resources = statement["Action"], statement["Resource"]
        found.append(
            (
                statement["Sid"],
                [actions] if isinstance(actions, str) else actions,
                [resources] if isinstance(resources, str) else resources,
            )
        )
    return found


BEDROCK_POLICIES = [path for path in IAM_POLICIES if path.name.startswith("bedrock-")]


@pytest.mark.parametrize("path", BEDROCK_POLICIES, ids=lambda path: path.name)
def test_bedrock_policies_grant_only_model_invocation(path: Path) -> None:
    for _, actions, resources in statements(path):
        assert actions == ["bedrock:InvokeModel"]
        assert resources and "*" not in resources
        assert all(resource.startswith("arn:aws:bedrock:") for resource in resources)
        assert any("foundation-model/<model-id>" in resource for resource in resources)


def test_logs_policy_grants_only_filter_log_events_on_named_log_groups() -> None:
    path = ROOT / "docs" / "iam" / "cloudwatch-logs-filter-log-events-policy.json"

    [(_, actions, resources)] = statements(path)

    assert actions == ["logs:FilterLogEvents"]  # no write, no Logs Insights, no wildcards
    assert resources and all(
        resource.startswith("arn:aws:logs:<region>:<account-id>:log-group:<log-group-name>")
        for resource in resources
    )
    assert not any(resource.rstrip(":*").endswith("*") for resource in resources)


def test_security_document_covers_every_security_requirement() -> None:
    security = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")

    for number in range(1, 9):
        assert f"SEC-{number}" in security
    assert "127.0.0.1" in security


def test_decision_records_are_numbered_without_gaps() -> None:
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    numbers = [int(value) for value in re.findall(r"^## ADR-(\d{3}):", decisions, re.M)]

    assert numbers == list(range(1, len(numbers) + 1))
    blocks = decisions.split("## ADR-")[1:]
    # Delivered MVP decisions remain accepted; future work may be proposed for owner review.
    assert all("**Status:** accepted" in block for block in blocks[:15])
    assert all(
        re.search(r"\*\*Status:\*\* (accepted|proposed|rejected|superseded)\b", block)
        for block in blocks
    )
