"""Regression tests for cancel / expire / cleanup races."""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app import config, main
from app import db as db_mod
from app import retention, waiters
from app.answers import STATUS_ANSWERED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_PENDING
from app.db import cancel_pending_run, db, get_run, resolve_callback


def _insert_pending(run_id: str | None = None, token: str | None = None) -> tuple[str, str]:
    correlation = run_id or f"run-{secrets.token_hex(8)}"
    tok = token or secrets.token_urlsafe(16)
    conn = db()
    try:
        conn.execute(
            "INSERT INTO runs (token, run_id, created_at, payload_json, status)"
            " VALUES (?,?,?,?,?)",
            (
                tok,
                correlation,
                datetime.now(timezone.utc).isoformat(),
                json.dumps({"message": "t"}),
                STATUS_PENDING,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return correlation, tok


async def _fake_ok_post(_self, url, **kwargs):
    return httpx.Response(
        200,
        json={"success": True},
        request=httpx.Request("POST", url),
    )


def test_callback_expire_does_not_overwrite_cancelled(monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", -1)
    run_id, token = _insert_pending()
    real_cas = db_mod.cas_expire

    def racing_cas(conn, rid: str) -> bool:
        conn.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?",
            (STATUS_CANCELLED, rid),
        )
        conn.commit()
        return real_cas(conn, rid)

    monkeypatch.setattr(db_mod, "cas_expire", racing_cas)
    result = resolve_callback(
        {"ok": True, "answer": "late", "run_id": run_id}, token
    )
    assert result["error"] == "already_cancelled"
    assert result["http_status"] == 409
    monkeypatch.setattr(db_mod, "cas_expire", real_cas)
    row = get_run(run_id)
    assert row is not None
    assert row[6] == STATUS_CANCELLED


def test_cancelled_callback_after_ttl_stays_cancelled(client, monkeypatch):
    monkeypatch.setattr(main.httpx.AsyncClient, "post", _fake_ok_post)
    created = json.loads(asyncio.run(main.ask_grokbot({"prompt": "c"}, wait_seconds=0)))
    json.loads(asyncio.run(main.cancel_run(created["run_id"])))
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", -1)
    path = urlparse(created["callback_url"]).path
    response = client.post(
        path, json={"ok": True, "answer": "late", "run_id": created["run_id"]}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "already_cancelled"
    record = json.loads(asyncio.run(main.get_grokbot_run(created["run_id"])))
    assert record["status"] == STATUS_CANCELLED
    assert record["answer_status"] == STATUS_CANCELLED


def test_cancel_expire_branch_respects_concurrent_answer(monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", -1)
    run_id, _token = _insert_pending()
    real_cas = db_mod.cas_expire

    def racing_cas(conn, rid: str) -> bool:
        conn.execute(
            "UPDATE runs SET status = ?, answer_json = ?, answered_at = ?"
            " WHERE run_id = ? AND status = ?",
            (
                STATUS_ANSWERED,
                json.dumps({"answer": "won"}),
                datetime.now(timezone.utc).isoformat(),
                rid,
                STATUS_PENDING,
            ),
        )
        conn.commit()
        return real_cas(conn, rid)

    monkeypatch.setattr(db_mod, "cas_expire", racing_cas)
    outcome, status = cancel_pending_run(run_id)
    assert outcome == "already_answered"
    assert status == STATUS_ANSWERED
    monkeypatch.setattr(db_mod, "cas_expire", real_cas)
    row = get_run(run_id)
    assert row is not None
    assert row[6] == STATUS_ANSWERED


def test_cleanup_notifies_on_event_loop_not_worker_thread(monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", -1)
    run_id, _token = _insert_pending()
    notify_threads: list[int] = []
    real_notify = waiters.notify_waiters

    def spy(rid: str) -> None:
        notify_threads.append(threading.current_thread().ident)
        real_notify(rid)

    monkeypatch.setattr(waiters, "notify_waiters", spy)

    retention.run_cleanup()
    assert notify_threads == []
    assert get_run(run_id)[6] == STATUS_EXPIRED

    run_id2, _token2 = _insert_pending()

    async def scenario() -> int:
        waiter = waiters.acquire_waiter(run_id2)
        loop_ident = threading.current_thread().ident
        stats = await retention.cleanup_once()
        assert run_id2 in stats["expired_run_ids"]
        assert waiter.is_set()
        return loop_ident

    loop_ident = asyncio.run(scenario())
    assert notify_threads
    assert all(ident == loop_ident for ident in notify_threads)


def test_get_and_list_lazy_expire(monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", -1)
    run_id, _token = _insert_pending()
    row = get_run(run_id)
    assert row is not None
    assert row[6] == STATUS_EXPIRED
    record = json.loads(asyncio.run(main.get_grokbot_run(run_id)))
    assert record["answer_status"] == STATUS_EXPIRED
    listed = json.loads(main.list_grokbot_runs(50))
    match = next(item for item in listed if item["run_id"] == run_id)
    assert match["status"] == STATUS_EXPIRED
    assert match["answer_status"] == STATUS_EXPIRED

    run_id2, _token2 = _insert_pending()
    listed2 = json.loads(main.list_grokbot_runs(50))
    match2 = next(item for item in listed2 if item["run_id"] == run_id2)
    assert match2["answer_status"] == STATUS_EXPIRED
