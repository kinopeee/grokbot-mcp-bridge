"""Secure MCP bridge to Grok Bot via the Cursor automation webhook.

Exposes an authenticated MCP endpoint (Streamable HTTP at /mcp, SSE at /sse)
that Poke can register as a custom MCP integration. The Cursor automation
webhook URL and its API key live only server-side; MCP clients can ask Grok Bot
questions and read inbound Grok Bot events without seeing those credentials.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import sqlite3
import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    import httpx
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    HAS_MCP = True
except ImportError:
    # Optional deps may be absent when a deployer only validates that `app`
    # exists; the MCP endpoints are registered only when they are installed.
    httpx = None
    MCPServer = None
    TransportSecuritySettings = None
    HAS_MCP = False

CURSOR_WEBHOOK_URL = os.environ.get("CURSOR_WEBHOOK_URL", "").strip()
CURSOR_WEBHOOK_API_KEY = os.environ.get("CURSOR_WEBHOOK_API_KEY", "").strip()
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()
INBOUND_WEBHOOK_SECRET = os.environ.get("INBOUND_WEBHOOK_SECRET", "").strip()
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "bridge.db"))
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL",
    f"https://{ALLOWED_HOSTS[0]}" if ALLOWED_HOSTS else "http://localhost:8080",
).rstrip("/")
CALLBACK_TTL_SECONDS = int(os.environ.get("CALLBACK_TTL_SECONDS", "3600"))
CALLBACK_ALLOW_HTTP = os.environ.get("CALLBACK_ALLOW_HTTP", "") == "1"
CALLBACK_ALLOWED_HOSTS = [
    h.strip().lower().rstrip(".")
    for h in os.environ.get("CALLBACK_ALLOWED_HOSTS", "").split(",")
    if h.strip()
]
MAX_EVENTS = 1000
MAX_CALLBACK_BODY = 256 * 1024
logger = logging.getLogger("grokbot-bridge")
_answer_waiters: dict[str, asyncio.Event] = {}

_PROTECTED_PREFIXES = ("/mcp", "/sse", "/messages")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
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
    return conn


def _store_event(delivery_id: str | None, event_type: str | None, signature_ok: bool,
                 headers: dict[str, str], raw_body: bytes) -> int:
    try:
        parsed = json.loads(raw_body)
        body_json = json.dumps(parsed)
    except Exception:
        body_json = None
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO events (received_at, delivery_id, event_type, signature_ok,"
            " headers_json, body_json, raw_body) VALUES (?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), delivery_id, event_type,
             1 if signature_ok else 0, json.dumps(headers), body_json,
             raw_body.decode("utf-8", "replace")),
        )
        conn.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
            (MAX_EVENTS,),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not INBOUND_WEBHOOK_SECRET or not signature_header:
        return False
    provided = signature_header.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):]
    expected = hmac.new(INBOUND_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


def _json_value(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _extract_answer_text(answer: Any) -> str | None:
    if isinstance(answer, str):
        return answer
    if not isinstance(answer, dict):
        return None
    for key in ("answer", "message", "content", "text", "output", "result"):
        value = answer.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if key == "content" and isinstance(value, list):
            text_blocks = [
                block["text"]
                for block in value
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()
            ]
            if text_blocks:
                return "\n".join(text_blocks)
    return None


def _run_callback_url(token: str) -> str:
    return f"{PUBLIC_BASE_URL}/callbacks/{token}"


def _run_record(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "run_id": row[0],
        "created_at": row[1],
        "payload": _json_value(row[2]),
        "upstream_status": row[3],
        "upstream_response": row[4],
        "run_uuid": row[5],
        "status": row[6],
        "answer": _json_value(row[7]),
        "answer_text": _extract_answer_text(_json_value(row[7])),
        "answered_at": row[8],
        "callback_url": _run_callback_url(row[9]),
    }


def _get_run(run_id: str) -> tuple[Any, ...] | None:
    conn = _db()
    try:
        return conn.execute(
            "SELECT run_id, created_at, payload_json, upstream_status, upstream_response,"
            " run_uuid, status, answer_json, answered_at, token FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()


def _run_answer(run_id: str) -> tuple[str, Any] | None:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT status, answer_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return row[0], _json_value(row[1])


def _host_matches(hostname: str, allowed_host: str) -> bool:
    return hostname == allowed_host or hostname.endswith(f".{allowed_host}")


def _is_safe_callback_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in (("https", "http") if CALLBACK_ALLOW_HTTP else ("https",)):
            return False, "scheme_not_allowed"
        if not hostname:
            return False, "hostname_required"
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            return False, "userinfo_not_allowed"
        try:
            port = parsed.port
        except ValueError:
            return False, "invalid_port"
        if not CALLBACK_ALLOW_HTTP and port not in (None, 80, 443):
            return False, "port_not_allowed"
        if CALLBACK_ALLOWED_HOSTS and not any(
            _host_matches(hostname, allowed) for allowed in CALLBACK_ALLOWED_HOSTS
        ):
            return False, "host_not_allowed"
        if any(_host_matches(hostname, own.lower().rstrip(".")) for own in ALLOWED_HOSTS):
            return False, "own_host_not_allowed"
        if hostname == "localhost" or hostname.endswith(".internal") or hostname.endswith(".local"):
            return False, "local_hostname_not_allowed"

        literal_address = None
        try:
            literal_address = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        addresses = [literal_address] if literal_address is not None else [
            ipaddress.ip_address(info[4][0])
            for info in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        ]
        if not CALLBACK_ALLOW_HTTP:
            for address in addresses:
                mapped = getattr(address, "ipv4_mapped", None)
                checked = mapped or address
                if (
                    checked.is_private
                    or checked.is_loopback
                    or checked.is_link_local
                    or checked.is_multicast
                    or checked.is_reserved
                    or checked.is_unspecified
                ):
                    return False, "private_address_not_allowed"
        return True, "ok"
    except (OSError, ValueError):
        return False, "dns_resolution_failed"


if HAS_MCP:
    mcp = MCPServer(
        "grokbot-bridge",
        instructions=(
            "Bridge to Grok Bot through the Cursor automation webhook. Use ask_grokbot "
            "to ask Grok Bot a question. Runs include callback_url/reply_url/"
            "response_url values that Grok Bot can POST to when an answer is ready; use "
            "get_grokbot_run, wait_for_grokbot_answer, and list_grokbot_runs to "
            "retrieve run status and answers. ask_grokbot waits up to wait_seconds "
            "(default 45) for the callback and returns answer_text. Grok Bot must "
            "echo run_id (or request_id) in the callback body. Use "
            "list_grokbot_events/get_grokbot_event to read inbound Grok Bot events "
            "received through the Cursor automation webhook."
        ),
    )
else:
    mcp = None


if mcp is not None:
    _tool = mcp.tool
else:
    def _tool(*_a, **_k):
        def deco(fn):
            return fn
        return deco


@_tool(structured_output=False)
async def bridge_status() -> str:
    """Report which credentials are configured on the bridge (booleans only, never values)."""
    conn = _db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    return json.dumps({
        "webhook_url_configured": bool(CURSOR_WEBHOOK_URL),
        "webhook_api_key_configured": bool(CURSOR_WEBHOOK_API_KEY),
        "inbound_webhook_secret_configured": bool(INBOUND_WEBHOOK_SECRET),
        "events_stored": count,
    })


@_tool(structured_output=False)
async def ask_grokbot(payload: dict[str, Any], wait_seconds: int = 45) -> str:
    """Ask Grok Bot through the configured Cursor automation webhook.

    The webhook URL and auth key are held server-side and never returned.
    A callback URL is added under callback_url, reply_url, and response_url unless
    the caller supplied those keys. Grok Bot can POST its answer to that callback;
    it must echo run_id (or request_id) in the callback body. This waits up to
    wait_seconds (default 45) for the callback and returns answer_text; pass 0
    to return without waiting.
    """
    correlation = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    callback_url = _run_callback_url(token)
    body = dict(payload)
    for key in ("callback_url", "reply_url", "response_url"):
        body.setdefault(key, callback_url)
    body["run_id"] = correlation
    body["request_id"] = correlation
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO runs (token, run_id, created_at, payload_json, status)"
            " VALUES (?,?,?,?,?)",
            (token, correlation, created_at, json.dumps(body), "pending"),
        )
        conn.execute(
            "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
            (MAX_EVENTS,),
        )
        conn.commit()
    finally:
        conn.close()

    result: dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "response": None,
        "run_id": correlation,
        "request_id": correlation,
        "callback_url": callback_url,
        "answer": None,
        "answer_text": None,
        "answer_status": "pending",
    }
    logger.info("run created run_id=%s", correlation)
    if not CURSOR_WEBHOOK_URL or not CURSOR_WEBHOOK_API_KEY:
        result.update({
            "error": "bridge_not_configured",
            "detail": "CURSOR_WEBHOOK_URL or CURSOR_WEBHOOK_API_KEY is not set on the server.",
            "summary": f"No answer yet from Grok Bot; call wait_for_grokbot_answer with run_id {correlation}.",
        })
        logger.info(
            "trigger returning run_id=%s answer_status=%s waited=%s",
            correlation, result["answer_status"], 0,
        )
        return json.dumps(result)
    headers = {
        "Authorization": f"Bearer {CURSOR_WEBHOOK_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(CURSOR_WEBHOOK_URL, json=body, headers=headers)
        body = resp.text[:2000]
        run_uuid = None
        try:
            upstream_json = resp.json()
            if isinstance(upstream_json, dict):
                run_uuid = upstream_json.get("runUuid")
        except (ValueError, TypeError):
            pass
        conn = _db()
        try:
            conn.execute(
                "UPDATE runs SET upstream_status = ?, upstream_response = ?, run_uuid = ?"
                " WHERE run_id = ?",
                (resp.status_code, body, run_uuid, correlation),
            )
            conn.commit()
        finally:
            conn.close()
        result.update({"ok": resp.is_success, "status_code": resp.status_code, "response": body})
    except httpx.HTTPError as exc:
        result.update({"error": "upstream_request_failed", "detail": str(exc)[:500]})

    timeout = max(0, min(int(wait_seconds), 120))
    waited = 0.0
    if timeout:
        waiter = asyncio.Event()
        _answer_waiters[correlation] = waiter
        started = asyncio.get_running_loop().time()
        try:
            while True:
                answer = _run_answer(correlation)
                if answer and answer[0] == "answered":
                    answer_text = _extract_answer_text(answer[1])
                    result.update({
                        "answer": answer[1],
                        "answer_text": answer_text,
                        "answer_status": "answered",
                        "summary": f"Grok Bot answered: {answer_text}",
                    })
                    break
                remaining = timeout - (asyncio.get_running_loop().time() - started)
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(waiter.wait(), min(0.5, remaining))
                except asyncio.TimeoutError:
                    pass
        finally:
            if _answer_waiters.get(correlation) is waiter:
                _answer_waiters.pop(correlation, None)
        waited = asyncio.get_running_loop().time() - started
    if result["answer_status"] != "answered":
        result["summary"] = (
            f"No answer yet from Grok Bot; call wait_for_grokbot_answer with run_id {correlation}."
        )
    logger.info(
        "trigger returning run_id=%s answer_status=%s waited=%s",
        correlation, result["answer_status"], round(waited, 3),
    )
    return json.dumps(result)


@_tool(structured_output=False)
async def get_grokbot_run(run_id: str) -> str:
    """Return one Grok Bot run by UUID, including raw answer and answer_text."""
    row = _get_run(run_id)
    if not row:
        return json.dumps({"error": "not_found", "run_id": run_id})
    return json.dumps(_run_record(row))


@_tool(structured_output=False)
async def wait_for_grokbot_answer(run_id: str, timeout_seconds: int = 60) -> str:
    """Wait for a Grok Bot callback by UUID and return raw answer plus answer_text."""
    timeout = max(1, min(int(timeout_seconds), 120))
    for _ in range(timeout):
        answer = _run_answer(run_id)
        if answer and answer[0] == "answered":
            return json.dumps({
                "run_id": run_id,
                "status": answer[0],
                "answer": answer[1],
                "answer_text": _extract_answer_text(answer[1]),
            })
        await asyncio.sleep(1)
    answer = _run_answer(run_id)
    if not answer:
        return json.dumps({"error": "not_found", "run_id": run_id})
    return json.dumps({
        "run_id": run_id,
        "status": answer[0],
        "answer": answer[1],
        "answer_text": _extract_answer_text(answer[1]),
    })


@_tool(structured_output=False)
def list_grokbot_runs(limit: int = 20) -> str:
    """List recent Grok Bot runs as summaries.

    The run_uuid field is the Cursor automation run UUID.
    """
    limit = max(1, min(int(limit), 100))
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT run_id, created_at, run_uuid, status, upstream_status FROM runs"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return json.dumps([
        {"run_id": row[0], "created_at": row[1], "run_uuid": row[2],
         "status": row[3], "upstream_status": row[4]}
        for row in rows
    ])


@_tool(structured_output=False)
def list_grokbot_events(limit: int = 20) -> str:
    """List recent inbound Grok Bot events received by the bridge (summaries only)."""
    limit = max(1, min(int(limit), 100))
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, received_at, delivery_id, event_type, signature_ok FROM events"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return json.dumps([
        {"id": r[0], "received_at": r[1], "delivery_id": r[2], "event_type": r[3],
         "signature_verified": bool(r[4])}
        for r in rows
    ])


@_tool(structured_output=False)
def get_grokbot_event(event_id: int) -> str:
    """Return the full stored payload of one inbound Grok Bot event by numeric id."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, received_at, delivery_id, event_type, signature_ok, body_json, raw_body"
            " FROM events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return json.dumps({"error": "not_found", "event_id": event_id})
    body = row[5] if row[5] is not None else json.dumps(row[6])
    return json.dumps({
        "id": row[0], "received_at": row[1], "delivery_id": row[2], "event_type": row[3],
        "signature_verified": bool(row[4]), "body": json.loads(body) if body else None,
    })


if mcp is not None:
    _resource = mcp.resource
else:
    def _resource(*_a, **_k):
        def deco(fn):
            return fn
        return deco


@_resource("grokbot://events", name="grokbot-events", mime_type="application/json")
def grokbot_events_resource() -> str:
    """Recent inbound Grok Bot events as a JSON document."""
    return list_grokbot_events(50)


@_resource("grokbot://runs", name="grokbot-runs", mime_type="application/json")
def grokbot_runs_resource() -> str:
    """Recent Grok Bot run summaries as a JSON document."""
    return list_grokbot_runs(50)


# --- ASGI wiring -------------------------------------------------------------

if mcp is not None:
    # Bearer auth already guards these routes; DNS-rebinding host checks only
    # matter for unauthenticated localhost servers, so allow the configured
    # public hosts (or disable the check if none are configured).
    if ALLOWED_HOSTS:
        _transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=ALLOWED_HOSTS + ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"],
            allowed_origins=[f"https://{h}" for h in ALLOWED_HOSTS],
        )
    else:
        _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    streamable_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, transport_security=_transport_security
    )
    sse_app = mcp.sse_app(
        sse_path="/sse", message_path="/messages/", transport_security=_transport_security
    )
    _mcp_lifespan = streamable_app.router.lifespan_context
else:
    streamable_app = None
    sse_app = None
    _mcp_lifespan = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if _mcp_lifespan is not None:
        async with _mcp_lifespan(app):
            yield
    else:
        yield


app = FastAPI()
app.router.lifespan_context = lifespan


@app.middleware("http")
async def require_mcp_auth(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in _PROTECTED_PREFIXES):
        if not MCP_API_KEY:
            return JSONResponse({"error": "mcp_auth_not_configured"}, status_code=503)
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            auth = auth[7:].strip()
        presented = auth or request.headers.get("x-api-key", "").strip()
        if not hmac.compare_digest(presented, MCP_API_KEY):
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def root():
    return {
        "service": "grokbot-mcp-bridge",
        "mcp_streamable_http": "/mcp",
        "mcp_sse": "/sse",
        "inbound_webhook": "/hooks/grokbot",
    }


async def _read_callback_body(request: Request) -> dict[str, Any] | JSONResponse:
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > MAX_CALLBACK_BODY:
            return JSONResponse({"error": "callback_body_too_large"}, status_code=413)
    except ValueError:
        return JSONResponse({"error": "invalid_content_length"}, status_code=400)
    raw_body = await request.body()
    if len(raw_body) > MAX_CALLBACK_BODY:
        return JSONResponse({"error": "callback_body_too_large"}, status_code=413)
    try:
        body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "json_object_required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json_object_required"}, status_code=400)
    return body


def _resolve_callback(body: dict[str, Any], token: str | None) -> JSONResponse | dict[str, Any]:
    echo = body.get("run_id") or body.get("request_id")
    if not isinstance(echo, str) or not echo:
        return JSONResponse({"error": "run_id_required"}, status_code=400)
    conn = _db()
    try:
        if token is not None:
            row = conn.execute(
                "SELECT id, run_id, created_at, status FROM runs WHERE token = ?", (token,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, run_id, created_at, status FROM runs WHERE run_id = ?", (echo,)
            ).fetchone()
        if not row:
            error = "unknown_callback" if token is not None else "unknown_run"
            return JSONResponse({"error": error}, status_code=404)
        if not hmac.compare_digest(str(row[1] or ""), echo):
            return JSONResponse({"error": "run_id_mismatch"}, status_code=400)
        if row[3] == "answered":
            return JSONResponse({"error": "already_answered"}, status_code=409)
        try:
            created_at = datetime.fromisoformat(row[2])
            expired = (
                datetime.now(timezone.utc) - created_at
            ).total_seconds() > CALLBACK_TTL_SECONDS
        except (TypeError, ValueError):
            expired = True
        if row[3] == "expired" or expired:
            conn.execute("UPDATE runs SET status = 'expired' WHERE run_id = ?", (row[1],))
            conn.commit()
            return JSONResponse({"error": "callback_expired"}, status_code=410)
        cur = conn.execute(
            "UPDATE runs SET status = 'answered', answer_json = ?, answered_at = ?"
            " WHERE run_id = ? AND status = 'pending'",
            (json.dumps(body), datetime.now(timezone.utc).isoformat(), row[1]),
        )
        conn.commit()
        if cur.rowcount == 0:
            status = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (row[1],)
            ).fetchone()
            if status and status[0] == "answered":
                return JSONResponse({"error": "already_answered"}, status_code=409)
            return JSONResponse({"error": "callback_expired"}, status_code=410)
    finally:
        conn.close()
    if waiter := _answer_waiters.get(row[1]):
        waiter.set()
    logger.info("callback resolved run_id=%s status=answered", row[1])
    return {"ok": True, "run_id": row[1]}


@app.post("/callbacks")
async def grokbot_callback_without_token(request: Request):
    """Capture an answer posted with only its correlation UUID."""
    body = await _read_callback_body(request)
    if isinstance(body, JSONResponse):
        return body
    return _resolve_callback(body, None)


@app.post("/callbacks/{token}")
async def grokbot_callback(token: str, request: Request):
    """Capture an answer posted to a pending outbound Grok Bot run."""
    body = await _read_callback_body(request)
    if isinstance(body, JSONResponse):
        return body
    return _resolve_callback(body, token)


@app.post("/hooks/grokbot")
async def inbound_grokbot_webhook(request: Request):
    """Receive Grok Bot events through the Cursor automation webhook."""
    if not INBOUND_WEBHOOK_SECRET:
        return JSONResponse({"error": "inbound_webhook_not_configured"}, status_code=503)
    raw_body = await request.body()
    signature = request.headers.get("x-webhook-signature")
    bearer = request.headers.get("authorization", "")
    signature_ok = _verify_signature(raw_body, signature) or hmac.compare_digest(
        bearer, f"Bearer {INBOUND_WEBHOOK_SECRET}")
    if not signature_ok:
        return JSONResponse({"error": "invalid_signature"}, status_code=401)
    safe_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() in ("x-webhook-id", "x-webhook-event", "user-agent", "content-type")
    }
    event_id = _store_event(
        delivery_id=request.headers.get("x-webhook-id"),
        event_type=request.headers.get("x-webhook-event"),
        signature_ok=True,
        headers=safe_headers,
        raw_body=raw_body,
    )
    try:
        parsed_body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed_body = None
    callback_url = None
    if isinstance(parsed_body, dict):
        callback_url = next(
            (
                parsed_body.get(key)
                for key in ("callback_url", "reply_url", "response_url")
                if isinstance(parsed_body.get(key), str) and parsed_body.get(key).strip()
            ),
            None,
        )
    if not callback_url:
        return JSONResponse({
            "ok": True,
            "event_id": event_id,
            "callback": "none",
            "note": "コールバックURLなし",
        })
    safe, reason = _is_safe_callback_url(callback_url)
    if not safe:
        return JSONResponse({
            "ok": True,
            "event_id": event_id,
            "callback": "rejected",
            "callback_status": None,
            "callback_error": reason,
        })
    callback_status = None
    callback_error = None
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(
                callback_url,
                json={"ok": True, "answer": f"受信しました (event_id={event_id})"},
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "grokbot-mcp-bridge",
                },
            )
        callback_status = response.status_code
        if not response.is_success:
            callback_error = f"callback_http_status_{response.status_code}"
            callback_result = "failed"
        else:
            callback_result = "delivered"
    except httpx.HTTPError as exc:
        callback_result = "failed"
        callback_error = f"callback_request_failed:{type(exc).__name__}"
    return JSONResponse({
        "ok": True,
        "event_id": event_id,
        "callback": callback_result,
        "callback_status": callback_status,
        "callback_error": callback_error,
    })


if streamable_app is not None:
    app.router.routes.extend(streamable_app.routes)
if sse_app is not None:
    app.router.routes.extend(sse_app.routes)


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
