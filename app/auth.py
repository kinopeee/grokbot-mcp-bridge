"""MCP Bearer auth and inbound webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from app import config
from app.rate_limit import bind_request_key

PROTECTED_PREFIXES = ("/mcp", "/sse", "/messages")


def is_protected_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES)


def presented_mcp_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        auth = auth[7:].strip()
    return auth or request.headers.get("x-api-key", "").strip()


async def require_mcp_auth(request: Request, call_next):
    path = request.url.path
    if is_protected_path(path):
        if not config.MCP_API_KEY:
            return JSONResponse({"error": "mcp_auth_not_configured"}, status_code=503)
        presented = presented_mcp_key(request)
        if not hmac.compare_digest(
            presented.encode("utf-8"), config.MCP_API_KEY.encode("utf-8")
        ):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        bind_request_key(presented)
    return await call_next(request)


def verify_signature(
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
) -> bool:
    if not config.INBOUND_WEBHOOK_SECRET or not signature_header:
        return False
    provided = signature_header.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256="):]
    signed_payload = raw_body
    if timestamp_header is not None:
        try:
            timestamp = int(timestamp_header.strip())
        except ValueError:
            return False
        if abs(time.time() - timestamp) > config.INBOUND_TIMESTAMP_TOLERANCE:
            return False
        signed_payload = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(
        config.INBOUND_WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def inbound_bearer_ok(authorization: str) -> bool:
    if not config.INBOUND_WEBHOOK_SECRET:
        return False
    return hmac.compare_digest(
        authorization.encode("utf-8"),
        f"Bearer {config.INBOUND_WEBHOOK_SECRET}".encode("utf-8"),
    )
