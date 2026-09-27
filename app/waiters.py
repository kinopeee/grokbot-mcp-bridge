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


def _event_loop(event: asyncio.Event) -> asyncio.AbstractEventLoop | None:
    getter = getattr(event, "_get_loop", None)
    if callable(getter):
        try:
            return getter()
        except RuntimeError:
            return None
    loop = getattr(event, "_loop", None)
    return loop if isinstance(loop, asyncio.AbstractEventLoop) else None


def notify_waiters(run_id: str) -> None:
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    for event in list(_waiters.get(run_id, ())):
        loop = _event_loop(event)
        if running is not None and (loop is None or loop is running):
            event.set()
            continue
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)
            continue
        event.set()
