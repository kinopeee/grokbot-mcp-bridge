"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ABSOLUTE_MAX_WAIT_SECONDS = 300
DEFAULT_RUN_RETENTION_SECONDS = 7 * 24 * 3600
DEFAULT_EVENT_RETENTION_SECONDS = 7 * 24 * 3600
DEFAULT_CLEANUP_INTERVAL_SECONDS = 300
DEFAULT_RATE_LIMIT_PER_MINUTE = 30
DEFAULT_MAX_WAIT_SECONDS = 180
DEFAULT_ASK_WAIT_SECONDS = 60
DEFAULT_WAIT_TIMEOUT_SECONDS = 60


def _int_env(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


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
CALLBACK_TTL_SECONDS = _int_env("CALLBACK_TTL_SECONDS", 3600, minimum=0)
CALLBACK_ALLOW_HTTP = os.environ.get("CALLBACK_ALLOW_HTTP", "") == "1"
CALLBACK_ALLOWED_HOSTS = [
    h.strip().lower().rstrip(".")
    for h in os.environ.get("CALLBACK_ALLOWED_HOSTS", "").split(",")
    if h.strip()
]
MAX_EVENTS = 1000
MAX_CALLBACK_BODY = 256 * 1024
INBOUND_TIMESTAMP_TOLERANCE = _int_env(
    "INBOUND_TIMESTAMP_TOLERANCE_SECONDS", 300, minimum=0
)
RUN_RETENTION_SECONDS = _int_env(
    "RUN_RETENTION_SECONDS", DEFAULT_RUN_RETENTION_SECONDS, minimum=0
)
EVENT_RETENTION_SECONDS = _int_env(
    "EVENT_RETENTION_SECONDS", DEFAULT_EVENT_RETENTION_SECONDS, minimum=0
)
CLEANUP_INTERVAL_SECONDS = _int_env(
    "CLEANUP_INTERVAL_SECONDS", DEFAULT_CLEANUP_INTERVAL_SECONDS, minimum=0
)
RATE_LIMIT_PER_MINUTE = _int_env(
    "RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT_PER_MINUTE, minimum=0
)
MAX_WAIT_SECONDS = _int_env(
    "MAX_WAIT_SECONDS",
    DEFAULT_MAX_WAIT_SECONDS,
    minimum=1,
    maximum=ABSOLUTE_MAX_WAIT_SECONDS,
)
PORT = _int_env("PORT", 8080, minimum=1, maximum=65535)
