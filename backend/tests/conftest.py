"""Test configuration for backend unit tests."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/testdb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("PALACEOFTRUTH_ADMIN_SECRET", "test-admin-secret")

import pytest
