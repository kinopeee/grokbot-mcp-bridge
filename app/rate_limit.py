"""In-process sliding-window rate limiter (per API-key hash or local)."""

from __future__ import annotations

import contextvars
import hashlib
import threading
import time
from collections import deque

from app import config

WINDOW_SECONDS = 60.0
LOCAL_KEY = "local"
current_limit_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "rate_limit_key", default=LOCAL_KEY
)


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        window_start = now - WINDOW_SECONDS
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] < window_start:
                hits.popleft()
            if len(hits) >= limit:
                retry = int(hits[0] + WINDOW_SECONDS - now) + 1
                return False, max(1, retry)
            hits.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def hash_presented_key(presented: str) -> str:
    return hashlib.sha256(presented.encode("utf-8")).hexdigest()


def bind_request_key(presented: str) -> None:
    current_limit_key.set(hash_presented_key(presented))


def check_write_limit(key: str | None = None) -> tuple[bool, int]:
    return limiter.allow(key or current_limit_key.get(), config.RATE_LIMIT_PER_MINUTE)


def rate_limited_payload(retry_after_seconds: int) -> dict[str, object]:
    return {
        "ok": False,
        "error": "rate_limited",
        "retry_after_seconds": retry_after_seconds,
        "summary": (
            f"Rate limited. Retry after {retry_after_seconds} seconds."
        ),
    }
