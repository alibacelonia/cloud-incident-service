import os

import pytest
from live_support import live_settings, live_skip_reason

from cloud_incident_service.config import Settings


@pytest.fixture
def bedrock_settings() -> Settings:
    """Live settings, or a skip with the exact reason live tests cannot run."""
    reason = live_skip_reason(os.environ)
    if reason is not None:
        pytest.skip(reason)
    settings = live_settings(os.environ)

    import boto3

    if boto3.session.Session(region_name=settings.aws_region).get_credentials() is None:
        pytest.skip("AWS credentials were not found in the default credential chain")
    return settings
