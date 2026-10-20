import socket

import pytest

ISOLATED_VARIABLES = (
    "AI_PROVIDER",
    "AI_MODEL",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "TRACE_DIR",
    "ORCHESTRATOR",
    "MODEL_MAX_OUTPUT_TOKENS",
    "SERVICE_API_KEY",
    "ANTHROPIC_API_KEY",
)


def is_live(request) -> bool:
    return request.node.get_closest_marker("live") is not None


@pytest.fixture(autouse=True)
def isolated_environment(request, monkeypatch, tmp_path):
    """Keep the developer's shell and AWS files from changing test outcomes."""
    if is_live(request):
        return  # Live tests deliberately use the developer's AWS configuration.
    for name in ISOLATED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


class NetworkAccessBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def block_network(request, monkeypatch):
    """Fail any default test that tries to open a network connection (NFR-1)."""
    if is_live(request):
        return

    def refuse(*_args, **_kwargs):
        raise NetworkAccessBlocked("Network access is disabled in the default test suite")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
