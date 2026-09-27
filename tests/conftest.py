"""Shared test env. Must set variables before `app` is imported."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ["MCP_API_KEY"] = "test-mcp"
os.environ["INBOUND_WEBHOOK_SECRET"] = "test-inbound"
os.environ["CURSOR_WEBHOOK_URL"] = "https://automation.invalid/hook"
os.environ["CURSOR_WEBHOOK_API_KEY"] = "x"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "bridge.db")
os.environ["ALLOWED_HOSTS"] = "testserver"
os.environ["CALLBACK_ALLOW_HTTP"] = "1"
os.environ["CALLBACK_ALLOWED_HOSTS"] = "example.com,example.test"
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"
os.environ["CLEANUP_INTERVAL_SECONDS"] = "0"

from app import main  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(main.app) as test_client:
        yield test_client
