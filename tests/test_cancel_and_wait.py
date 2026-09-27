"""cancel_run, answer_status consistency, and wait UX."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

import httpx

from app import config, main
from app.answers import STATUS_ANSWERED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_PENDING


async def _fake_ok_post(_self, url, **kwargs):
    return httpx.Response(
        200,
        json={"success": True},
        request=httpx.Request("POST", url),
    )


def test_cancel_pending_unblocks_waiters(monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)

    async def scenario():
        created = json.loads(await main.ask_grokbot({"prompt": "c"}, wait_seconds=0))
        run_id = created["run_id"]
        waiter = asyncio.create_task(
            main.wait_for_grokbot_answer(run_id, timeout_seconds=5)
        )
        await asyncio.sleep(0.05)
        cancelled = json.loads(await main.cancel_run(run_id))
        waited = json.loads(await waiter)
        return created, cancelled, waited

    created, cancelled, waited = asyncio.run(scenario())
    assert cancelled["ok"] is True
    assert cancelled["answer_status"] == STATUS_CANCELLED
    assert cancelled["already_cancelled"] is False
    assert waited["answer_status"] == STATUS_CANCELLED
    assert waited["status"] == STATUS_CANCELLED
    assert "cancelled" in waited["summary"]
    record = json.loads(asyncio.run(main.get_grokbot_run(created["run_id"])))
    assert record["status"] == STATUS_CANCELLED
    assert record["answer_status"] == STATUS_CANCELLED
    listed = json.loads(main.list_grokbot_runs(20))
    match = next(item for item in listed if item["run_id"] == created["run_id"])
    assert match["status"] == STATUS_CANCELLED
    assert match["answer_status"] == STATUS_CANCELLED


def test_cancel_is_idempotent(monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)
    created = json.loads(asyncio.run(main.ask_grokbot({"prompt": "idemp"}, wait_seconds=0)))
    first = json.loads(asyncio.run(main.cancel_run(created["run_id"])))
    second = json.loads(asyncio.run(main.cancel_run(created["run_id"])))
    assert first["ok"] is True
    assert first["already_cancelled"] is False
    assert second["ok"] is True
    assert second["already_cancelled"] is True
    assert second["answer_status"] == STATUS_CANCELLED


def test_cancel_already_answered(client, monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)
    created = json.loads(asyncio.run(main.ask_grokbot({"prompt": "ans"}, wait_seconds=0)))
    path = urlparse(created["callback_url"]).path
    assert client.post(
        path, json={"ok": True, "answer": "done", "run_id": created["run_id"]}
    ).status_code == 200
    result = json.loads(asyncio.run(main.cancel_run(created["run_id"])))
    assert result["ok"] is False
    assert result["error"] == "already_answered"
    assert result["answer_status"] == STATUS_ANSWERED


def test_cancel_not_found():
    result = json.loads(asyncio.run(main.cancel_run("does-not-exist")))
    assert result["error"] == "not_found"


def test_callback_on_cancelled_is_409(client, monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)
    created = json.loads(asyncio.run(main.ask_grokbot({"prompt": "x"}, wait_seconds=0)))
    json.loads(asyncio.run(main.cancel_run(created["run_id"])))
    path = urlparse(created["callback_url"]).path
    response = client.post(
        path, json={"ok": True, "answer": "late", "run_id": created["run_id"]}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "already_cancelled"


def test_ask_grokbot_reports_cancelled_while_waiting(monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)

    async def scenario():
        known = {row["run_id"] for row in json.loads(main.list_grokbot_runs(100))}
        task = asyncio.create_task(
            main.ask_grokbot({"prompt": "wait-cancel"}, wait_seconds=5)
        )
        deadline = time.monotonic() + 2
        run_id = None
        while time.monotonic() < deadline:
            rows = json.loads(main.list_grokbot_runs(20))
            fresh = [row["run_id"] for row in rows if row["run_id"] not in known]
            if fresh:
                run_id = fresh[0]
                break
            await asyncio.sleep(0.02)
        assert run_id
        await main.cancel_run(run_id)
        return json.loads(await task)

    result = asyncio.run(scenario())
    assert result["answer_status"] == STATUS_CANCELLED
    assert "cancelled" in result["summary"]


def test_pending_summary_mentions_wait_and_cancel(monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)
    result = json.loads(asyncio.run(
        main.ask_grokbot({"prompt": "pending"}, wait_seconds=1)
    ))
    assert result["answer_status"] == STATUS_PENDING
    assert "wait_for_grokbot_answer" in result["summary"]
    assert "cancel_run" in result["summary"]
    assert result["run_id"] in result["summary"]
    assert "answer_status=pending" in result["summary"]


def test_wait_returns_expired_status(monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)
    created = json.loads(asyncio.run(
        main.ask_grokbot({"prompt": "exp"}, wait_seconds=0)
    ))
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", -1)
    waited = json.loads(asyncio.run(
        main.wait_for_grokbot_answer(created["run_id"], timeout_seconds=1)
    ))
    assert waited["answer_status"] == STATUS_EXPIRED
    assert waited["status"] == STATUS_EXPIRED
    assert "expired" in waited["summary"]


def test_get_run_echoes_answer_status(monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)
    created = json.loads(asyncio.run(
        main.ask_grokbot({"prompt": "echo"}, wait_seconds=0)
    ))
    record = json.loads(asyncio.run(main.get_grokbot_run(created["run_id"])))
    assert record["status"] == STATUS_PENDING
    assert record["answer_status"] == STATUS_PENDING
    assert "wait_for_grokbot_answer" in record["summary"]
