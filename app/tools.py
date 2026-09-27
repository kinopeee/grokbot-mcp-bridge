"""MCP tools and resources exposed to Poke."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app import config
from app.answers import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    TERMINAL_STATUSES,
    clamp_wait_seconds,
    extract_answer_text,
    status_summary,
)
from app.db import (
    cancel_pending_run,
    count_events,
    get_event,
    get_run,
    insert_run,
    list_events,
    list_runs,
    run_answer,
    run_callback_url,
    run_record,
    update_run_upstream,
)
from app.logging_util import logger
from app.mcp_app import resource, tool
from app.rate_limit import check_write_limit, rate_limited_payload
from app.waiters import acquire_waiter, notify_waiters, release_waiter


async def _wait_for_run(run_id: str, timeout: float) -> tuple[str, Any] | None:
    waiter = acquire_waiter(run_id)
    started = asyncio.get_running_loop().time()
    try:
        while True:
            answer = await asyncio.to_thread(run_answer, run_id)
            if answer is None:
                return None
            if answer[0] in TERMINAL_STATUSES:
                return answer
            remaining = timeout - (asyncio.get_running_loop().time() - started)
            if remaining <= 0:
                return answer
            try:
                await asyncio.wait_for(waiter.wait(), min(0.5, remaining))
            except asyncio.TimeoutError:
                pass
            if waiter.is_set():
                release_waiter(run_id, waiter)
                waiter = acquire_waiter(run_id)
    finally:
        release_waiter(run_id, waiter)


@tool(structured_output=False)
async def bridge_status() -> str:
    """Report which credentials are configured on the bridge (booleans only, never values)."""
    count = await asyncio.to_thread(count_events)
    return json.dumps({
        "webhook_url_configured": bool(config.CURSOR_WEBHOOK_URL),
        "webhook_api_key_configured": bool(config.CURSOR_WEBHOOK_API_KEY),
        "inbound_webhook_secret_configured": bool(config.INBOUND_WEBHOOK_SECRET),
        "events_stored": count,
    })


@tool(structured_output=False)
async def ask_grokbot(
    payload: dict[str, Any],
    wait_seconds: int = config.DEFAULT_ASK_WAIT_SECONDS,
) -> str:
    """Ask Grok Bot through the configured Cursor automation webhook.

    The webhook URL and auth key are held server-side and never returned.
    A callback URL is added under callback_url, reply_url, and response_url unless
    the caller supplied those keys. Grok Bot can POST its answer to that callback;
    it must echo run_id (or request_id) in the callback body.

    This waits up to wait_seconds for the callback and returns answer_text plus
    answer_status (pending / answered / cancelled / expired). Default wait is 60
    seconds; the value is clamped to 0..MAX_WAIT_SECONDS (default 180, hard cap
    300). Pass 0 to return without waiting. If the result is pending, call
    wait_for_grokbot_answer with the returned run_id, or cancel_run to stop.
    Write calls are rate-limited per API key (RATE_LIMIT_PER_MINUTE).
    """
    allowed, retry_after = check_write_limit()
    if not allowed:
        return json.dumps(rate_limited_payload(retry_after))

    correlation = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    callback_url = run_callback_url(token)
    body = dict(payload)
    for key in ("callback_url", "reply_url", "response_url"):
        body.setdefault(key, callback_url)
    body["run_id"] = correlation
    body["request_id"] = correlation
    created_at = datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(insert_run, token, correlation, created_at, body)

    result: dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "response": None,
        "run_id": correlation,
        "request_id": correlation,
        "callback_url": callback_url,
        "answer": None,
        "answer_text": None,
        "answer_status": STATUS_PENDING,
    }
    logger.info("run created run_id=%s", correlation)
    if not config.CURSOR_WEBHOOK_URL or not config.CURSOR_WEBHOOK_API_KEY:
        result.update({
            "error": "bridge_not_configured",
            "detail": "CURSOR_WEBHOOK_URL or CURSOR_WEBHOOK_API_KEY is not set on the server.",
            "summary": status_summary(correlation, STATUS_PENDING),
        })
        logger.info(
            "trigger returning run_id=%s answer_status=%s waited=%s",
            correlation, result["answer_status"], 0,
        )
        return json.dumps(result)
    headers = {
        "Authorization": f"Bearer {config.CURSOR_WEBHOOK_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(config.CURSOR_WEBHOOK_URL, json=body, headers=headers)
        response_text = resp.text[:2000]
        run_uuid = None
        try:
            upstream_json = resp.json()
            if isinstance(upstream_json, dict):
                run_uuid = upstream_json.get("runUuid")
        except (ValueError, TypeError):
            pass
        await asyncio.to_thread(
            update_run_upstream, correlation, resp.status_code, response_text, run_uuid
        )
        result.update({
            "ok": resp.is_success,
            "status_code": resp.status_code,
            "response": response_text,
        })
    except httpx.HTTPError as exc:
        result.update({"error": "upstream_request_failed", "detail": type(exc).__name__})

    timeout = clamp_wait_seconds(wait_seconds, allow_zero=True)
    waited = 0.0
    if timeout:
        started = asyncio.get_running_loop().time()
        answer = await _wait_for_run(correlation, timeout)
        waited = asyncio.get_running_loop().time() - started
        if answer and answer[0] in TERMINAL_STATUSES:
            answer_text = extract_answer_text(answer[1])
            result.update({
                "answer": answer[1],
                "answer_text": answer_text,
                "answer_status": answer[0],
                "summary": status_summary(
                    correlation, answer[0], answer_text=answer_text
                ),
            })
    if result["answer_status"] == STATUS_PENDING:
        result["summary"] = status_summary(correlation, STATUS_PENDING)
    logger.info(
        "trigger returning run_id=%s answer_status=%s waited=%s",
        correlation, result["answer_status"], round(waited, 3),
    )
    return json.dumps(result)


@tool(structured_output=False)
async def get_grokbot_run(run_id: str) -> str:
    """Return one Grok Bot run by UUID, including raw answer, answer_text, and answer_status.

    answer_status matches status and is one of pending, answered, cancelled, expired.
    """
    row = get_run(run_id)
    if not row:
        return json.dumps({"error": "not_found", "run_id": run_id})
    record = run_record(row)
    record["summary"] = status_summary(
        run_id, record["answer_status"], answer_text=record.get("answer_text")
    )
    return json.dumps(record)


@tool(structured_output=False)
async def wait_for_grokbot_answer(
    run_id: str,
    timeout_seconds: int = config.DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> str:
    """Wait for a Grok Bot callback (or cancel/expiry) by UUID.

    Returns raw answer plus answer_text and answer_status (pending / answered /
    cancelled / expired). timeout_seconds is clamped to 1..MAX_WAIT_SECONDS
    (default 180, hard cap 300). If the run is already terminal, returns
    immediately. If still pending after the timeout, call this tool again with
    the same run_id, or cancel_run to stop waiting.
    """
    timeout = clamp_wait_seconds(timeout_seconds, allow_zero=False)
    answer = await _wait_for_run(run_id, float(timeout))
    if not answer:
        return json.dumps({"error": "not_found", "run_id": run_id})
    status, payload = answer
    answer_text = extract_answer_text(payload)
    return json.dumps({
        "run_id": run_id,
        "status": status,
        "answer_status": status,
        "answer": payload,
        "answer_text": answer_text,
        "summary": status_summary(run_id, status, answer_text=answer_text),
    })


@tool(structured_output=False)
async def cancel_run(run_id: str) -> str:
    """Cancel a pending Grok Bot run so waiters unblock with answer_status=cancelled.

    Duplicate cancel of an already-cancelled run is idempotent (ok=true).
    A run that is already answered or expired is not cancelled (error already_answered
    or already_expired). Write calls are rate-limited per API key.
    """
    allowed, retry_after = check_write_limit()
    if not allowed:
        return json.dumps(rate_limited_payload(retry_after))

    outcome, status = await asyncio.to_thread(cancel_pending_run, run_id)
    if outcome == "not_found":
        return json.dumps({"error": "not_found", "run_id": run_id})
    if outcome == "already_answered":
        return json.dumps({
            "ok": False,
            "error": "already_answered",
            "run_id": run_id,
            "status": status,
            "answer_status": status,
            "summary": status_summary(run_id, status or STATUS_ANSWERED),
        })
    if outcome == "already_expired":
        notify_waiters(run_id)
        return json.dumps({
            "ok": False,
            "error": "already_expired",
            "run_id": run_id,
            "status": STATUS_EXPIRED,
            "answer_status": STATUS_EXPIRED,
            "summary": status_summary(run_id, STATUS_EXPIRED),
        })
    notify_waiters(run_id)
    already = outcome == "already_cancelled"
    return json.dumps({
        "ok": True,
        "run_id": run_id,
        "status": STATUS_CANCELLED,
        "answer_status": STATUS_CANCELLED,
        "already_cancelled": already,
        "summary": status_summary(run_id, STATUS_CANCELLED),
    })


@tool(structured_output=False)
def list_grokbot_runs(limit: int = 20) -> str:
    """List recent Grok Bot runs as summaries.

    The run_uuid field is the Cursor automation run UUID.
    status and answer_status are the same value: pending, answered, cancelled, or expired.
    """
    limit = max(1, min(int(limit), 100))
    rows = list_runs(limit)
    return json.dumps([
        {
            "run_id": row[0],
            "created_at": row[1],
            "run_uuid": row[2],
            "status": row[3],
            "answer_status": row[3],
            "upstream_status": row[4],
        }
        for row in rows
    ])


@tool(structured_output=False)
def list_grokbot_events(limit: int = 20) -> str:
    """List recent inbound Grok Bot events received by the bridge (summaries only)."""
    limit = max(1, min(int(limit), 100))
    rows = list_events(limit)
    return json.dumps([
        {"id": r[0], "received_at": r[1], "delivery_id": r[2], "event_type": r[3],
         "signature_verified": bool(r[4])}
        for r in rows
    ])


@tool(structured_output=False)
def get_grokbot_event(event_id: int) -> str:
    """Return the full stored payload of one inbound Grok Bot event by numeric id."""
    row = get_event(event_id)
    if not row:
        return json.dumps({"error": "not_found", "event_id": event_id})
    body = row[5] if row[5] is not None else json.dumps(row[6])
    return json.dumps({
        "id": row[0], "received_at": row[1], "delivery_id": row[2], "event_type": row[3],
        "signature_verified": bool(row[4]), "body": json.loads(body) if body else None,
    })


@resource("grokbot://events", name="grokbot-events", mime_type="application/json")
def grokbot_events_resource() -> str:
    """Recent inbound Grok Bot events as a JSON document."""
    return list_grokbot_events(50)


@resource("grokbot://runs", name="grokbot-runs", mime_type="application/json")
def grokbot_runs_resource() -> str:
    """Recent Grok Bot run summaries as a JSON document."""
    return list_grokbot_runs(50)
