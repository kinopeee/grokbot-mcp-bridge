"""Startup and periodic SQLite cleanup (WAL-safe, short-lived connections)."""

from __future__ import annotations

import asyncio

from app import config
from app.db import prune_store
from app.logging_util import logger
from app.waiters import notify_waiters


def run_cleanup() -> dict:
    stats = prune_store()
    for run_id in stats.get("expired_run_ids") or []:
        notify_waiters(run_id)
    logger.info(
        "cleanup pruned expired_runs=%s deleted_runs=%s deleted_events=%s",
        stats.get("expired_runs"),
        stats.get("deleted_runs"),
        stats.get("deleted_events"),
    )
    return stats


async def cleanup_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_cleanup)
        except Exception:
            logger.exception("cleanup failed")
        interval = config.CLEANUP_INTERVAL_SECONDS
        if interval <= 0:
            return
        await asyncio.sleep(interval)
