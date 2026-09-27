"""SQLite persistence (WAL). Connections are opened per call and always closed."""

from __future__ import annotations

import hmac
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app import config
from app.answers import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    extract_answer_text,
)

_schema_ready: set[str] = set()

TERMINAL_RUN_STATUSES = ("answered", "cancelled", "expired")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    if config.DB_PATH not in _schema_ready:
        ensure_schema(conn)
        _schema_ready.add(config.DB_PATH)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            delivery_id TEXT,
            event_type TEXT,
            signature_ok INTEGER NOT NULL,
            headers_json TEXT NOT NULL,
            body_json TEXT,
            raw_body TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            run_id TEXT,
            created_at TEXT NOT NULL,
            payload_json TEXT,
            upstream_status INTEGER,
            upstream_response TEXT,
            run_uuid TEXT,
            status TEXT NOT NULL,
            answer_json TEXT,
            answered_at TEXT
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "run_id" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN run_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_run_id ON runs(run_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_status_created ON runs(status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_received_at ON events(received_at)"
    )


def json_value(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def run_callback_url(token: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/callbacks/{token}"


def run_record(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    status = row[6]
    return {
        "run_id": row[0],
        "created_at": row[1],
        "payload": json_value(row[2]),
        "upstream_status": row[3],
        "upstream_response": row[4],
        "run_uuid": row[5],
        "status": status,
        "answer_status": status,
        "answer": json_value(row[7]),
        "answer_text": extract_answer_text(json_value(row[7])),
        "answered_at": row[8],
        "callback_url": run_callback_url(row[9]),
    }


def _select_run_row(conn: sqlite3.Connection, run_id: str) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT run_id, created_at, payload_json, upstream_status, upstream_response,"
        " run_uuid, status, answer_json, answered_at, token FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()


def cas_expire(conn: sqlite3.Connection, run_id: str) -> bool:
    """Transition pending → expired. Returns True only if this call won the CAS."""
    cur = conn.execute(
        "UPDATE runs SET status = ? WHERE run_id = ? AND status = ?",
        (STATUS_EXPIRED, run_id, STATUS_PENDING),
    )
    return bool(cur.rowcount)


def expire_stale_pending(
    conn: sqlite3.Connection, ttl_seconds: int | None = None
) -> list[str]:
    """Expire pending rows past TTL. Never touches cancelled/answered."""
    expired_ids: list[str] = []
    pending_rows = conn.execute(
        "SELECT run_id, created_at FROM runs WHERE status = ?",
        (STATUS_PENDING,),
    ).fetchall()
    for run_id, created_at in pending_rows:
        if is_created_expired(created_at, ttl_seconds) and cas_expire(conn, run_id):
            expired_ids.append(run_id)
    return expired_ids


def callback_error_for_status(status: str) -> dict[str, Any]:
    if status == STATUS_ANSWERED:
        return {"error": "already_answered", "http_status": 409}
    if status == STATUS_CANCELLED:
        return {"error": "already_cancelled", "http_status": 409}
    return {"error": "callback_expired", "http_status": 410}


def get_run(run_id: str) -> tuple[Any, ...] | None:
    conn = db()
    try:
        row = _select_run_row(conn, run_id)
        if not row:
            return None
        if row[6] == STATUS_PENDING and is_created_expired(row[1]):
            cas_expire(conn, run_id)
            conn.commit()
            row = _select_run_row(conn, run_id)
        return row
    finally:
        conn.close()


def is_created_expired(created_at: str, ttl_seconds: int | None = None) -> bool:
    ttl = config.CALLBACK_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    try:
        created = datetime.fromisoformat(created_at)
        return (datetime.now(timezone.utc) - created).total_seconds() > ttl
    except (TypeError, ValueError):
        return True


def run_answer(run_id: str) -> tuple[str, Any] | None:
    conn = db()
    try:
        row = conn.execute(
            "SELECT status, answer_json, created_at FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        status, answer_json, created_at = row
        if status == STATUS_PENDING and is_created_expired(created_at):
            cur = conn.execute(
                "UPDATE runs SET status = ? WHERE run_id = ? AND status = ?",
                (STATUS_EXPIRED, run_id, STATUS_PENDING),
            )
            conn.commit()
            if cur.rowcount:
                status = STATUS_EXPIRED
            else:
                refreshed = conn.execute(
                    "SELECT status, answer_json FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if refreshed:
                    status, answer_json = refreshed
        return status, json_value(answer_json)
    finally:
        conn.close()


def insert_run(token: str, correlation: str, created_at: str, body: dict[str, Any]) -> None:
    conn = db()
    try:
        conn.execute(
            "INSERT INTO runs (token, run_id, created_at, payload_json, status)"
            " VALUES (?,?,?,?,?)",
            (token, correlation, created_at, json.dumps(body), STATUS_PENDING),
        )
        _cap_terminal_runs(conn)
        conn.commit()
    finally:
        conn.close()


def update_run_upstream(
    correlation: str, status_code: int, response_text: str, run_uuid: str | None
) -> None:
    conn = db()
    try:
        conn.execute(
            "UPDATE runs SET upstream_status = ?, upstream_response = ?, run_uuid = ?"
            " WHERE run_id = ?",
            (status_code, response_text, run_uuid, correlation),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_pending_run(run_id: str) -> tuple[str, str | None]:
    """Cancel a pending run.

    Returns (result, status) where result is one of:
    cancelled, already_cancelled, already_answered, already_expired, not_found.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    try:
        row = conn.execute(
            "SELECT status, created_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return "not_found", None
        status, created_at = row
        if status == STATUS_PENDING and is_created_expired(created_at):
            won = cas_expire(conn, run_id)
            conn.commit()
            if won:
                return "already_expired", STATUS_EXPIRED
            again = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not again:
                return "not_found", None
            status = again[0]
        if status == STATUS_CANCELLED:
            return "already_cancelled", STATUS_CANCELLED
        if status == STATUS_ANSWERED:
            return "already_answered", STATUS_ANSWERED
        if status == STATUS_EXPIRED:
            return "already_expired", STATUS_EXPIRED
        cur = conn.execute(
            "UPDATE runs SET status = ?, answered_at = ? WHERE run_id = ? AND status = ?",
            (STATUS_CANCELLED, now, run_id, STATUS_PENDING),
        )
        conn.commit()
        if cur.rowcount == 0:
            again = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not again:
                return "not_found", None
            current = again[0]
            if current == STATUS_CANCELLED:
                return "already_cancelled", STATUS_CANCELLED
            if current == STATUS_ANSWERED:
                return "already_answered", STATUS_ANSWERED
            return "already_expired", STATUS_EXPIRED
        return "cancelled", STATUS_CANCELLED
    finally:
        conn.close()


def store_event(
    delivery_id: str | None,
    event_type: str | None,
    signature_ok: bool,
    headers: dict[str, str],
    raw_body: bytes,
) -> int:
    try:
        parsed = json.loads(raw_body)
        body_json = json.dumps(parsed)
    except Exception:
        body_json = None
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO events (received_at, delivery_id, event_type, signature_ok,"
            " headers_json, body_json, raw_body) VALUES (?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                delivery_id,
                event_type,
                1 if signature_ok else 0,
                json.dumps(headers),
                body_json,
                raw_body.decode("utf-8", "replace"),
            ),
        )
        conn.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
            (config.MAX_EVENTS,),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def count_events() -> int:
    conn = db()
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def list_runs(limit: int) -> list[tuple[Any, ...]]:
    conn = db()
    try:
        expire_stale_pending(conn)
        conn.commit()
        return conn.execute(
            "SELECT run_id, created_at, run_uuid, status, upstream_status FROM runs"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def list_events(limit: int) -> list[tuple[Any, ...]]:
    conn = db()
    try:
        return conn.execute(
            "SELECT id, received_at, delivery_id, event_type, signature_ok FROM events"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def get_event(event_id: int) -> tuple[Any, ...] | None:
    conn = db()
    try:
        return conn.execute(
            "SELECT id, received_at, delivery_id, event_type, signature_ok, body_json, raw_body"
            " FROM events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
    finally:
        conn.close()


def resolve_callback(body: dict[str, Any], token: str) -> dict[str, Any]:
    """Resolve a token-scoped callback. Returns a result dict (ok or error)."""
    echo = body.get("run_id") or body.get("request_id")
    if not isinstance(echo, str) or not echo:
        return {"error": "run_id_required", "http_status": 400}
    conn = db()
    try:
        row = conn.execute(
            "SELECT id, run_id, created_at, status FROM runs WHERE token = ?", (token,)
        ).fetchone()
        if not row:
            return {"error": "unknown_callback", "http_status": 404}
        if not hmac.compare_digest(
            str(row[1] or "").encode("utf-8"), echo.encode("utf-8")
        ):
            return {"error": "run_id_mismatch", "http_status": 400}
        if row[3] == STATUS_ANSWERED:
            return callback_error_for_status(STATUS_ANSWERED)
        if row[3] == STATUS_CANCELLED:
            return callback_error_for_status(STATUS_CANCELLED)
        if row[3] == STATUS_EXPIRED or is_created_expired(row[2]):
            if row[3] == STATUS_EXPIRED:
                return callback_error_for_status(STATUS_EXPIRED)
            won = cas_expire(conn, row[1])
            conn.commit()
            if won:
                return callback_error_for_status(STATUS_EXPIRED)
            status = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (row[1],)
            ).fetchone()
            return callback_error_for_status(status[0] if status else STATUS_EXPIRED)
        cur = conn.execute(
            "UPDATE runs SET status = 'answered', answer_json = ?, answered_at = ?"
            " WHERE run_id = ? AND status = ?",
            (
                json.dumps(body),
                datetime.now(timezone.utc).isoformat(),
                row[1],
                STATUS_PENDING,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            status = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (row[1],)
            ).fetchone()
            return callback_error_for_status(status[0] if status else STATUS_EXPIRED)
    finally:
        conn.close()
    return {"ok": True, "run_id": row[1], "http_status": 200}


def _cap_terminal_runs(conn: sqlite3.Connection) -> None:
    placeholders = ",".join("?" * len(TERMINAL_RUN_STATUSES))
    conn.execute(
        f"DELETE FROM runs WHERE status IN ({placeholders})"
        " AND id NOT IN ("
        f" SELECT id FROM runs WHERE status IN ({placeholders})"
        " ORDER BY id DESC LIMIT ?"
        ")",
        (*TERMINAL_RUN_STATUSES, *TERMINAL_RUN_STATUSES, config.MAX_EVENTS),
    )


def prune_store(
    *,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
    run_retention_seconds: int | None = None,
    event_retention_seconds: int | None = None,
) -> dict[str, Any]:
    """Expire stale pending runs and delete old terminal runs / events.

    Never deletes a run that is still pending within CALLBACK_TTL_SECONDS.
    """
    moment = now or datetime.now(timezone.utc)
    ttl = config.CALLBACK_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    run_keep = (
        config.RUN_RETENTION_SECONDS
        if run_retention_seconds is None
        else run_retention_seconds
    )
    event_keep = (
        config.EVENT_RETENTION_SECONDS
        if event_retention_seconds is None
        else event_retention_seconds
    )
    expire_before = (moment - timedelta(seconds=ttl)).isoformat()
    run_cutoff = (moment - timedelta(seconds=run_keep)).isoformat()
    event_cutoff = (moment - timedelta(seconds=event_keep)).isoformat()
    conn = db()
    try:
        expired_ids = expire_stale_pending(conn, ttl)
        deleted_runs = 0
        deleted_events = 0
        if run_keep > 0:
            deleted_runs = conn.execute(
                "DELETE FROM runs WHERE status IN (?, ?, ?)"
                " AND COALESCE(answered_at, created_at) < ?",
                (*TERMINAL_RUN_STATUSES, run_cutoff),
            ).rowcount
        if event_keep > 0:
            deleted_events = conn.execute(
                "DELETE FROM events WHERE received_at < ?",
                (event_cutoff,),
            ).rowcount
        conn.commit()
    finally:
        conn.close()
    return {
        "expired_runs": len(expired_ids),
        "deleted_runs": int(deleted_runs or 0),
        "deleted_events": int(deleted_events or 0),
        "expired_run_ids": expired_ids,
        "expire_before": expire_before,
        "run_cutoff": run_cutoff,
        "event_cutoff": event_cutoff,
    }
