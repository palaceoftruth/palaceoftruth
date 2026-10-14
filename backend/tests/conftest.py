"""Test configuration for backend unit tests."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/testdb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("PALACEOFTRUTH_ADMIN_SECRET", "test-admin-secret")

import pytest

# Known ambient configuration names a developer may export for real work. Unit
# tests must be hermetic and must NOT inherit them; a narrow name whitelist is
# deleted per test instead of clearing the whole environment. Tests that need a
# value set it explicitly with monkeypatch.setenv.
_AMBIENT_CONFIG_NAMES = (
    "TYPESAFE_API_KEY",
    "PALACEOFTRUTH_BASE_URL",
    "PALACEOFTRUTH_API_BASE_URL",
    "PALACEOFTRUTH_API_KEY",
    "PALACEOFTRUTH_DEFAULT_SCOPE_TYPE",
    "PALACEOFTRUTH_DEFAULT_SCOPE_KEY",
    "PALACEOFTRUTH_MCP_OAUTH_RESOURCE",
    "PALACEOFTRUTH_MCP_OAUTH_AUDIENCE",
    "PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL",
    "PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET",
    "PALACEOFTRUTH_MCP_CLIENT_KEY",
    "PALACEOFTRUTH_MCP_CLIENT_NAME",
    "PALACEOFTRUTH_MCP_CLIENT_SCOPES",
    "SECONDBRAIN_BASE_URL",
    "SECONDBRAIN_API_BASE_URL",
    "SECONDBRAIN_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_ambient_config(monkeypatch):
    for name in _AMBIENT_CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
