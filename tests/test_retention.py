"""SQLite retention / prune behavior."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from app import config, main
from app.answers import STATUS_EXPIRED, STATUS_PENDING
from app.db import db, get_run, insert_run, prune_store, store_event


def _insert_run_at(
    *,
    created_at: str,
    status: str = STATUS_PENDING,
    answered_at: str | None = None,
    run_id: str | None = None,
) -> str:
    correlation = run_id or f"run-{secrets.token_hex(8)}"
    token = secrets.token_urlsafe(16)
    conn = db()
    try:
        conn.execute(
            "INSERT INTO runs (token, run_id, created_at, payload_json, status, answered_at)"
            " VALUES (?,?,?,?,?,?)",
            (token, correlation, created_at, json.dumps({"message": "t"}), status, answered_at),
        )
        conn.commit()
    finally:
        conn.close()
    return correlation


def test_prune_keeps_pending_within_ttl(monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", 3600)
    monkeypatch.setattr(config, "RUN_RETENTION_SECONDS", 1)
    created = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    run_id = _insert_run_at(created_at=created, status=STATUS_PENDING)
    stats = prune_store()
    assert get_run(run_id) is not None
    assert get_run(run_id)[6] == STATUS_PENDING
    assert stats["expired_runs"] == 0
    assert run_id not in stats["expired_run_ids"]


def test_prune_expires_stale_pending_but_does_not_delete_recent_expired(monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", 60)
    monkeypatch.setattr(config, "RUN_RETENTION_SECONDS", 7 * 24 * 3600)
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    run_id = _insert_run_at(created_at=created, status=STATUS_PENDING)
    stats = prune_store()
    row = get_run(run_id)
    assert row is not None
    assert row[6] == STATUS_EXPIRED
    assert stats["expired_runs"] == 1
    assert run_id in stats["expired_run_ids"]


def test_prune_deletes_old_answered_runs_and_events(monkeypatch):
    monkeypatch.setattr(config, "RUN_RETENTION_SECONDS", 60)
    monkeypatch.setattr(config, "EVENT_RETENTION_SECONDS", 60)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    run_id = _insert_run_at(
        created_at=old, status="answered", answered_at=old
    )
    event_id = store_event(None, "old", True, {"content-type": "application/json"}, b"{}")
    conn = db()
    try:
        conn.execute("UPDATE events SET received_at = ? WHERE id = ?", (old, event_id))
        conn.commit()
    finally:
        conn.close()
    stats = prune_store()
    assert get_run(run_id) is None
    assert stats["deleted_runs"] >= 1
    assert stats["deleted_events"] >= 1


def test_prune_keeps_recent_answered(monkeypatch):
    monkeypatch.setattr(config, "RUN_RETENTION_SECONDS", 3600)
    now = datetime.now(timezone.utc).isoformat()
    run_id = _insert_run_at(created_at=now, status="answered", answered_at=now)
    prune_store()
    assert get_run(run_id) is not None


def test_prune_retention_zero_skips_delete(monkeypatch):
    monkeypatch.setattr(config, "RUN_RETENTION_SECONDS", 0)
    monkeypatch.setattr(config, "EVENT_RETENTION_SECONDS", 0)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    run_id = _insert_run_at(created_at=old, status="answered", answered_at=old)
    stats = prune_store()
    assert get_run(run_id) is not None
    assert stats["deleted_runs"] == 0
    assert stats["deleted_events"] == 0


def test_prune_retains_just_expired_pending_older_than_retention(monkeypatch):
    """A pending row older than both TTL and retention must survive the prune that expires it."""
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", 60)
    monkeypatch.setattr(config, "RUN_RETENTION_SECONDS", 60)
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    run_id = _insert_run_at(created_at=created, status=STATUS_PENDING)
    stats = prune_store()
    row = get_run(run_id)
    assert row is not None
    assert row[6] == STATUS_EXPIRED
    assert row[8] is not None
    assert stats["expired_runs"] == 1
    assert run_id in stats["expired_run_ids"]
    prune_store()
    assert get_run(run_id) is not None


def test_prune_stamps_legacy_expired_null_answered_at(monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", 60)
    monkeypatch.setattr(config, "RUN_RETENTION_SECONDS", 60)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    run_id = _insert_run_at(created_at=old, status=STATUS_EXPIRED, answered_at=None)
    prune_store()
    row = get_run(run_id)
    assert row is not None
    assert row[6] == STATUS_EXPIRED
    assert row[8] is not None


def test_prune_deletes_expired_after_retention_from_answered_at(monkeypatch):
    monkeypatch.setattr(config, "CALLBACK_TTL_SECONDS", 60)
    monkeypatch.setattr(config, "RUN_RETENTION_SECONDS", 60)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    run_id = _insert_run_at(created_at=old, status=STATUS_EXPIRED, answered_at=old)
    stats = prune_store()
    assert get_run(run_id) is None
    assert stats["deleted_runs"] >= 1


def test_insert_run_cap_does_not_drop_pending():
    insert_run(secrets.token_urlsafe(8), "keep-pending", datetime.now(timezone.utc).isoformat(), {})
    assert get_run("keep-pending") is not None
    assert main.prune_store()["expired_runs"] >= 0
