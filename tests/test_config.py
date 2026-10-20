import pytest

from cloud_incident_service.config import ConfigurationError, Settings


def test_defaults_to_mock_provider() -> None:
    settings = Settings.from_environment({})

    assert settings.provider == "mock"
    assert settings.model == ""
    assert settings.aws_region is None


def test_provider_name_is_normalized() -> None:
    assert Settings.from_environment({"AI_PROVIDER": "  MOCK "}).provider == "mock"


def test_unknown_provider_names_variable_without_echoing_value() -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"AI_PROVIDER": "secret-looking-value"})

    assert raised.value.errors == ["AI_PROVIDER: Input should be 'mock', 'bedrock' or 'anthropic'"]
    assert "secret-looking-value" not in str(raised.value)


def test_bedrock_requires_model_and_region() -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"AI_PROVIDER": "bedrock"})

    assert raised.value.errors == ["AI_PROVIDER=bedrock requires AI_MODEL, AWS_REGION"]


def test_bedrock_accepts_default_region_fallback() -> None:
    settings = Settings.from_environment(
        {
            "AI_PROVIDER": "bedrock",
            "AI_MODEL": "example.model-v1:0",
            "AWS_DEFAULT_REGION": "eu-west-1",
        }
    )

    assert settings.aws_region == "eu-west-1"


def test_aws_region_takes_precedence_over_default_region() -> None:
    settings = Settings.from_environment(
        {"AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "eu-west-1"}
    )

    assert settings.aws_region == "us-east-1"


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({"AWS_REGION": "not a region"}, "AWS_REGION"),
        ({"AWS_REGION": "US-EAST-1"}, "AWS_REGION"),
        ({"AI_MODEL": "model with spaces"}, "AI_MODEL"),
        ({"AI_MODEL": "m" * 257}, "AI_MODEL"),
    ],
)
def test_malformed_values_name_the_variable(environ: dict[str, str], variable: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment(environ)

    assert len(raised.value.errors) == 1
    assert raised.value.errors[0].startswith(f"{variable}: ")
    for value in environ.values():
        assert value not in str(raised.value)


def test_reports_every_invalid_variable() -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"AI_PROVIDER": "nope", "AWS_REGION": "nowhere"})

    assert [error.split(":")[0] for error in raised.value.errors] == ["AI_PROVIDER", "AWS_REGION"]


def test_missing_bedrock_settings_are_reported_alongside_field_errors() -> None:
    with pytest.raises(ConfigurationError) as raised:
        Settings.from_environment({"AI_PROVIDER": "bedrock", "AWS_REGION": "Mars-1"})

    assert [error.split(":")[0] for error in raised.value.errors] == [
        "AWS_REGION",
        "AI_PROVIDER=bedrock requires AI_MODEL",
    ]
