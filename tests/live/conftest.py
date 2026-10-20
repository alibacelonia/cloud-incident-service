import os

import pytest
from live_support import credentials_skip_reason, live_settings, live_skip_reason

from cloud_incident_service.config import Settings


@pytest.fixture
def live_model_settings() -> Settings:
    """Live settings, or a skip with the exact reason live tests cannot run."""
    reason = live_skip_reason(os.environ)
    if reason is not None:
        pytest.skip(reason)
    settings = live_settings(os.environ)

    if settings.provider == "bedrock":
        import boto3

        reason = credentials_skip_reason(boto3.session.Session(region_name=settings.aws_region))
        if reason is not None:
            pytest.skip(reason)
    return settings
