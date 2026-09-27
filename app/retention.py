"""Startup and periodic SQLite cleanup (WAL-safe, short-lived connections)."""

from __future__ import annotations

import asyncio

from app import config
from app.db import prune_store
from app.logging_util import logger
from app import waiters


def run_cleanup() -> dict:
    """DB-only prune. Does not touch asyncio.Event (not thread-safe)."""
    stats = prune_store()
    logger.info(
        "cleanup pruned expired_runs=%s deleted_runs=%s deleted_events=%s",
        stats.get("expired_runs"),
        stats.get("deleted_runs"),
        stats.get("deleted_events"),
    )
    return stats


def notify_expired_runs(stats: dict) -> None:
    for run_id in stats.get("expired_run_ids") or []:
        waiters.notify_waiters(run_id)


async def cleanup_once() -> dict:
    stats = await asyncio.to_thread(run_cleanup)
    notify_expired_runs(stats)
    return stats


async def cleanup_loop() -> None:
    while True:
        interval = config.CLEANUP_INTERVAL_SECONDS
        if interval <= 0:
            return
        await asyncio.sleep(interval)
        try:
            await cleanup_once()
        except Exception:
            logger.exception("cleanup failed")
