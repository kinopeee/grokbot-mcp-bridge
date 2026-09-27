"""HTTP routes: health, inbound webhook, and token-scoped callbacks."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from app import config
from app.auth import inbound_bearer_ok, verify_signature
from app.db import resolve_callback, store_event
from app.logging_util import logger
from app.ssrf import inspect_callback_url
from app.waiters import notify_waiters


async def read_limited_body(request: Request) -> bytes | JSONResponse:
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > config.MAX_CALLBACK_BODY:
            return JSONResponse({"error": "body_too_large"}, status_code=413)
    except ValueError:
        return JSONResponse({"error": "invalid_content_length"}, status_code=400)
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > config.MAX_CALLBACK_BODY:
                return JSONResponse({"error": "body_too_large"}, status_code=413)
            body += chunk
    except ClientDisconnect:
        return JSONResponse({"error": "client_disconnected"}, status_code=400)
    return bytes(body)


async def read_callback_body(request: Request) -> dict[str, Any] | JSONResponse:
    raw_body = await read_limited_body(request)
    if isinstance(raw_body, JSONResponse):
        return raw_body
    try:
        body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "json_object_required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json_object_required"}, status_code=400)
    return body


def finish_callback_result(result: dict[str, Any]) -> JSONResponse | dict[str, Any]:
    status = int(result.pop("http_status", 200))
    if result.get("ok") and result.get("run_id"):
        notify_waiters(result["run_id"])
        logger.info("callback resolved run_id=%s status=answered", result["run_id"])
        return result
    return JSONResponse({"error": result.get("error", "callback_failed")}, status_code=status)


def register_routes(application: FastAPI) -> None:
    @application.get("/healthz")
    async def healthz():
        return {"ok": True}

    @application.get("/")
    async def root():
        return {
            "service": "grokbot-mcp-bridge",
            "mcp_streamable_http": "/mcp",
            "mcp_sse": "/sse",
            "inbound_webhook": "/hooks/grokbot",
        }

    @application.post("/callbacks/{token}")
    async def grokbot_callback(token: str, request: Request):
        """Capture an answer posted to a pending outbound Grok Bot run."""
        body = await read_callback_body(request)
        if isinstance(body, JSONResponse):
            return body
        result = await asyncio.to_thread(resolve_callback, body, token)
        return finish_callback_result(result)

    @application.post("/hooks/grokbot")
    async def inbound_grokbot_webhook(request: Request):
        """Receive Grok Bot events through the Cursor automation webhook."""
        if not config.INBOUND_WEBHOOK_SECRET:
            return JSONResponse({"error": "inbound_webhook_not_configured"}, status_code=503)
        raw_body = await read_limited_body(request)
        if isinstance(raw_body, JSONResponse):
            return raw_body
        signature = request.headers.get("x-webhook-signature")
        timestamp = request.headers.get("x-webhook-timestamp")
        bearer = request.headers.get("authorization", "")
        signature_ok = verify_signature(raw_body, signature, timestamp) or inbound_bearer_ok(
            bearer
        )
        if not signature_ok:
            return JSONResponse({"error": "invalid_signature"}, status_code=401)
        safe_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() in ("x-webhook-id", "x-webhook-event", "user-agent", "content-type")
        }
        event_id = await asyncio.to_thread(
            store_event,
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
        safe, reason, addresses = await asyncio.to_thread(
            inspect_callback_url, callback_url
        )
        if not safe:
            return JSONResponse({
                "ok": True,
                "event_id": event_id,
                "callback": "rejected",
                "callback_status": None,
                "callback_error": reason,
            })
        parsed_callback = urlparse(callback_url)
        callback_hostname = parsed_callback.hostname or ""
        pinned_address = addresses[0]
        pinned_netloc = (
            f"[{pinned_address}]" if ":" in pinned_address else pinned_address
        )
        if parsed_callback.port is not None:
            pinned_netloc += f":{parsed_callback.port}"
        pinned_url = urlunparse((
            parsed_callback.scheme,
            pinned_netloc,
            parsed_callback.path or "/",
            parsed_callback.params,
            parsed_callback.query,
            "",
        ))
        default_port = 443 if parsed_callback.scheme == "https" else 80
        host_header = callback_hostname
        if parsed_callback.port not in (None, default_port):
            host_header += f":{parsed_callback.port}"
        callback_status = None
        callback_error = None
        try:
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream(
                    "POST",
                    pinned_url,
                    json={"ok": True, "answer": f"受信しました (event_id={event_id})"},
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "grokbot-mcp-bridge",
                        "Host": host_header,
                    },
                    extensions={"sni_hostname": callback_hostname}
                    if parsed_callback.scheme == "https"
                    else None,
                ) as response:
                    callback_status = response.status_code
                    callback_success = response.is_success
            if not callback_success:
                callback_error = f"callback_http_status_{callback_status}"
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
