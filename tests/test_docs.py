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
    ]


@pytest.mark.parametrize("path", IAM_POLICIES, ids=lambda path: path.name)
def test_iam_policies_grant_only_model_invocation(path: Path) -> None:
    policy = json.loads(path.read_text(encoding="utf-8"))

    assert policy["Version"] == "2012-10-17"
    for statement in policy["Statement"]:
        actions = statement["Action"]
        resources = statement["Resource"]
        actions = [actions] if isinstance(actions, str) else actions
        resources = [resources] if isinstance(resources, str) else resources
        assert statement["Effect"] == "Allow"
        assert actions == ["bedrock:InvokeModel"]
        assert resources and "*" not in resources
        assert all(resource.startswith("arn:aws:bedrock:") for resource in resources)
        assert any("foundation-model/<model-id>" in resource for resource in resources)


def test_security_document_covers_every_security_requirement() -> None:
    security = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")

    for number in range(1, 9):
        assert f"SEC-{number}" in security
    assert "127.0.0.1" in security


def test_decision_records_are_numbered_without_gaps() -> None:
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    numbers = [int(value) for value in re.findall(r"^## ADR-(\d{3}):", decisions, re.M)]

    assert numbers == list(range(1, len(numbers) + 1))
    assert all("**Status:** accepted" in block for block in decisions.split("## ADR-")[1:])
