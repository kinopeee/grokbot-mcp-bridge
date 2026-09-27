"""Answer-text extraction and wait/status summaries for Poke."""

from __future__ import annotations

from typing import Any

from app import config

STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUSES = (
    STATUS_PENDING,
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
)
TERMINAL_STATUSES = frozenset(
    {STATUS_ANSWERED, STATUS_CANCELLED, STATUS_EXPIRED}
)


def extract_answer_text(answer: Any) -> str | None:
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


def clamp_wait_seconds(wait_seconds: int, *, allow_zero: bool = True) -> int:
    high = config.MAX_WAIT_SECONDS
    value = int(wait_seconds)
    if allow_zero:
        return max(0, min(value, high))
    return max(1, min(value, high))


def status_summary(
    run_id: str,
    status: str,
    *,
    answer_text: str | None = None,
) -> str:
    if status == STATUS_ANSWERED:
        return f"Grok Bot answered: {answer_text}"
    if status == STATUS_CANCELLED:
        return (
            f"Run {run_id} was cancelled (answer_status=cancelled). "
            "Do not wait; start a new ask_grokbot if you still need an answer."
        )
    if status == STATUS_EXPIRED:
        return (
            f"Run {run_id} expired before Grok Bot answered "
            "(answer_status=expired). Start a new ask_grokbot."
        )
    return (
        f"No answer yet from Grok Bot (answer_status=pending). "
        f"Call wait_for_grokbot_answer with run_id {run_id} "
        f"(timeout_seconds up to {config.MAX_WAIT_SECONDS}), "
        "or cancel_run to stop this run."
    )
