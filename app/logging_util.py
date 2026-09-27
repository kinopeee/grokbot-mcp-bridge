"""Process-wide logging: quiet HTTP clients and redact callback tokens."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("grokbot-bridge")
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_CALLBACK_PATH_RE = re.compile(r"(/callbacks/)[^?\s]+")


class RedactCallbackTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _CALLBACK_PATH_RE.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _CALLBACK_PATH_RE.sub(r"\1[redacted]", record.msg)
        return True


def install_access_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if any(isinstance(existing, RedactCallbackTokenFilter) for existing in access.filters):
        return
    access.addFilter(RedactCallbackTokenFilter())
