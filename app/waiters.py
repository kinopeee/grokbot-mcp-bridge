"""In-process waiters that unblock ask/wait/cancel without polling only."""

from __future__ import annotations

import asyncio

_waiters: dict[str, set[asyncio.Event]] = {}


def acquire_waiter(run_id: str) -> asyncio.Event:
    event = asyncio.Event()
    _waiters.setdefault(run_id, set()).add(event)
    return event


def release_waiter(run_id: str, event: asyncio.Event) -> None:
    group = _waiters.get(run_id)
    if not group:
        return
    group.discard(event)
    if not group:
        _waiters.pop(run_id, None)


def notify_waiters(run_id: str) -> None:
    for event in list(_waiters.get(run_id, ())):
        event.set()
