"""Secure MCP bridge to Grok Bot via the Cursor automation webhook.

Public entrypoint: `app.main:app` / `app.main:main`. Implementation lives in
focused modules (auth, db, tools, routes, ssrf, rate_limit, retention).
Names re-exported here keep existing tests and `from app.main import ...` working.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import config
from app.answers import extract_answer_text
from app.auth import PROTECTED_PREFIXES, require_mcp_auth, verify_signature
from app.db import (
    db as _db,
    get_run as _get_run,
    insert_run as _insert_run,
    json_value as _json_value,
    prune_store,
    resolve_callback as _resolve_callback_data,
    run_answer as _run_answer,
    run_callback_url as _run_callback_url,
    run_record as _run_record,
    store_event as _store_event,
    update_run_upstream as _update_run_upstream,
)
from app.logging_util import RedactCallbackTokenFilter, install_access_log_redaction
from app.mcp_app import HAS_MCP, build_mcp_apps, mcp
from app.retention import cleanup_loop, cleanup_once
from app.routes import read_limited_body as _read_limited_body, register_routes
from app.ssrf import host_matches as _host_matches
from app.ssrf import inspect_callback_url as _inspect_callback_url
from app.ssrf import is_safe_callback_url as _is_safe_callback_url
from app.tools import (
    ask_grokbot,
    bridge_status,
    cancel_run,
    get_grokbot_event,
    get_grokbot_run,
    list_grokbot_events,
    list_grokbot_runs,
    wait_for_grokbot_answer,
)

install_access_log_redaction()

CURSOR_WEBHOOK_URL = config.CURSOR_WEBHOOK_URL
CURSOR_WEBHOOK_API_KEY = config.CURSOR_WEBHOOK_API_KEY
MCP_API_KEY = config.MCP_API_KEY
INBOUND_WEBHOOK_SECRET = config.INBOUND_WEBHOOK_SECRET
DB_PATH = config.DB_PATH
ALLOWED_HOSTS = config.ALLOWED_HOSTS
PUBLIC_BASE_URL = config.PUBLIC_BASE_URL
CALLBACK_TTL_SECONDS = config.CALLBACK_TTL_SECONDS
CALLBACK_ALLOW_HTTP = config.CALLBACK_ALLOW_HTTP
CALLBACK_ALLOWED_HOSTS = config.CALLBACK_ALLOWED_HOSTS
MAX_EVENTS = config.MAX_EVENTS
MAX_CALLBACK_BODY = config.MAX_CALLBACK_BODY
INBOUND_TIMESTAMP_TOLERANCE = config.INBOUND_TIMESTAMP_TOLERANCE

_PROTECTED_PREFIXES = PROTECTED_PREFIXES
_RedactCallbackTokenFilter = RedactCallbackTokenFilter
_extract_answer_text = extract_answer_text
_verify_signature = verify_signature

streamable_app, sse_app, _mcp_lifespan = build_mcp_apps()


def _resolve_callback(body: dict, token: str) -> JSONResponse | dict:
    result = _resolve_callback_data(body, token)
    status = int(result.pop("http_status", 200))
    if result.get("ok"):
        return result
    return JSONResponse({"error": result.get("error", "callback_failed")}, status_code=status)


@contextlib.asynccontextmanager
async def lifespan(application: FastAPI):
    cleanup_task = None
    await cleanup_once()
    if config.CLEANUP_INTERVAL_SECONDS > 0:
        cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        if _mcp_lifespan is not None:
            async with _mcp_lifespan(application):
                yield
        else:
            yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task


def create_app() -> FastAPI:
    application = FastAPI()
    application.router.lifespan_context = lifespan
    application.middleware("http")(require_mcp_auth)
    register_routes(application)
    if streamable_app is not None:
        application.router.routes.extend(streamable_app.routes)
    if sse_app is not None:
        application.router.routes.extend(sse_app.routes)
    return application


app = create_app()


def main() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
