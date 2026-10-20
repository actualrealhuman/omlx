# SPDX-License-Identifier: Apache-2.0
"""Cooperative suspension for participating batched generation only."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable


class GenerationSuspendedError(RuntimeError):
    """Retryable rejection of new generation, distinct from cancellation."""


class GenerationGate:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.paused_snapshot = False
        self.restricted_snapshot = False
        self.reason = ""
        self._waiters: dict[asyncio.Event, asyncio.AbstractEventLoop] = {}

    def set_state(self, *, paused: bool, restricted: bool, reason: str = "") -> None:
        with self._condition:
            changed = paused != self.paused_snapshot
            self.reason = reason
            self.restricted_snapshot = restricted
            self.paused_snapshot = paused
            waiters = list(self._waiters.items()) if changed else []
            if changed:
                self._condition.notify_all()
        for event, loop in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(event.set)

    def check_admission(self) -> None:
        if self.paused_snapshot:
            raise GenerationSuspendedError(self.reason or "battery protection")

    async def wait(self, wake_event: asyncio.Event | None = None) -> None:
        """Wake for resume, lifecycle/maintenance, or a bounded reaper tick."""
        event = asyncio.Event()
        with self._condition:
            if not self.paused_snapshot:
                return
            self._waiters[event] = asyncio.get_running_loop()
        tasks = [asyncio.create_task(event.wait())]
        if wake_event is not None:
            tasks.append(asyncio.create_task(wake_event.wait()))
        try:
            await asyncio.wait(tasks, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
        finally:
            with self._condition:
                self._waiters.pop(event, None)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def wait_sync(self, cancelled: Callable[[], bool]) -> bool:
        """An in-call checkpoint; the caller supplies its safe abort boundary."""
        while True:
            if cancelled():
                return False
            with self._condition:
                if not self.paused_snapshot:
                    return True
                self._condition.wait(0.1)


_GATE = GenerationGate()


def get_generation_gate() -> GenerationGate:
    return _GATE
