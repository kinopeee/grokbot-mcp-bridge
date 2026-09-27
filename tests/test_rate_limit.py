"""Rate limiting for write tools and MCP-authenticated POST paths."""

from __future__ import annotations

import asyncio
import json

import httpx

from app import config, main
from app.rate_limit import limiter


def test_ask_grokbot_rate_limited(monkeypatch):
    limiter.reset()
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 1)

    async def fake_post(_self, url, **kwargs):
        return httpx.Response(
            200,
            json={"success": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(main.httpx.AsyncClient, "post", fake_post)
    first = json.loads(asyncio.run(main.ask_grokbot({"prompt": "a"}, wait_seconds=0)))
    second = json.loads(asyncio.run(main.ask_grokbot({"prompt": "b"}, wait_seconds=0)))
    assert first.get("error") != "rate_limited"
    assert second["error"] == "rate_limited"
    assert second["retry_after_seconds"] >= 1
    assert "Retry after" in second["summary"]
    limiter.reset()


def test_cancel_run_rate_limited(monkeypatch):
    limiter.reset()
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 1)
    first = json.loads(asyncio.run(main.cancel_run("missing-1")))
    second = json.loads(asyncio.run(main.cancel_run("missing-2")))
    assert first["error"] == "not_found"
    assert second["error"] == "rate_limited"
    limiter.reset()


def test_mcp_post_returns_429(client, monkeypatch):
    limiter.reset()
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 1)
    headers = {
        "x-api-key": "test-mcp",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    first = client.post("/mcp", headers=headers, json=body)
    second = client.post("/mcp", headers=headers, json=body)
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"] == "rate_limited"
    assert "Retry-After" in second.headers
    limiter.reset()
