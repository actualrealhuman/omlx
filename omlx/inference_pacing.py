# SPDX-License-Identifier: Apache-2.0
"""Shared best-effort submission pacing for batched generation engines.

The controller coordinates scheduler-call opportunities, not GPU execution.
An admitted call may outlive its work phase, and asynchronous work already
queued on an MLX stream may continue during REST.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count

MAX_PACING_DEFERRAL_S = 1.0
DEFAULT_WORK_QUANTUM_S = 0.4


class _PacingDisabled:
    """Result used when live policy changes to full speed during acquisition."""


PACING_DISABLED = _PacingDisabled()


@dataclass(frozen=True)
class PacingPermit:
    """One registered executor handoff; completion belongs to its worker."""

    engine_id: str
    generation: int
    lifecycle: int
    permit_id: int
    forced: bool = False


class InferencePacer:
    """Coordinate WORK/DRAIN/REST phases across participating engines.

    ``share`` is a requested submission share. The controller grants work
    opportunities based on union wall intervals marked by the scheduler; it
    does not estimate hardware utilization or enforce a power limit.
    """

    def __init__(
        self,
        share: float = 1.0,
        *,
        work_quantum_s: float = DEFAULT_WORK_QUANTUM_S,
        max_deferral_s: float = MAX_PACING_DEFERRAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._work_quantum_s = float(work_quantum_s)
        self._max_deferral_s = float(max_deferral_s)
        self._validate_share(share)
        self._share = float(share)
        # Read directly on the engine hot path: True means no admission,
        # timing, permit or accounting work is needed for this burst.
        self.full_speed_snapshot = self._share >= 1.0
        self._generation = 0
        self._phase = "work"
        self._phase_started: float | None = None
        self._work_deadline: float | None = None
        self._rest_deadline: float | None = None
        self._active: dict[int, PacingPermit] = {}
        self._permit_ids = count(1)
        self._engine_lifecycle: dict[str, int] = {}
        self._forced_started: set[int] = set()
        self._busy_engines: set[str] = set()
        self._cancelled_engines: set[str] = set()
        self._waiters: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
        self._deferred_since: dict[str, float] = {}
        self._activity_intervals: list[tuple[float, float]] = []
        self._work_union_s = 0.0
        self._missed_targets = 0

    @staticmethod
    def _validate_share(value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("inference share must be a finite number")
        if not math.isfinite(float(value)) or not 0.1 <= float(value) <= 1.0:
            raise ValueError("inference share must be between 0.1 and 1.0")

    @property
    def share(self) -> float:
        with self._lock:
            return self._share

    def snapshot(self) -> dict[str, float | int | str]:
        with self._lock:
            return {
                "share": self._share,
                "phase": self._phase,
                "generation": self._generation,
                "covered_work_union_seconds": self._work_union_s,
                "missed_targets": self._missed_targets,
            }

    def set_share(self, share: float) -> None:
        """Apply a live policy and invalidate old phase permits/deadlines."""
        self._validate_share(share)
        with self._lock:
            was_full_speed = self.full_speed_snapshot
            self._share = float(share)
            self._generation += 1
            now = self._clock()
            if self._share >= 1.0:
                self._deferred_since.clear()
                self._busy_engines.clear()
            elif was_full_speed:
                # A waiter that missed the brief full-speed generation still
                # gets a fresh, bounded deadline from re-enabling pacing.
                self._deferred_since.update(
                    {engine_id: now for engine_id in self._waiters}
                )
            self._reset_phase_locked(now)
            self._wake_all_locked()
            # Publish only after policy state is fully reset. Readers never
            # acquire the coordinator lock to check this immutable bool.
            self.full_speed_snapshot = self._share >= 1.0

    def cancel_engine(self, engine_id: str) -> None:
        """Wake an engine's async gate; an executing permit remains registered."""
        with self._lock:
            self._cancelled_engines.add(engine_id)
            self._deferred_since.pop(engine_id, None)
            self._wake_one_locked(engine_id)
            self._advance_locked(self._clock())

    def start_engine(self, engine_id: str) -> None:
        """Start a new lifecycle without reviving permits from an earlier run."""
        with self._lock:
            self._engine_lifecycle[engine_id] = (
                self._engine_lifecycle.get(engine_id, 0) + 1
            )
            self._cancelled_engines.discard(engine_id)
            self._deferred_since.pop(engine_id, None)
            self._wake_one_locked(engine_id)

    def set_engine_busy(self, engine_id: str, busy: bool) -> None:
        """Track scheduler demand so idle time cannot create delayed rest."""
        if self.full_speed_snapshot:
            return
        with self._lock:
            if self.full_speed_snapshot:
                return
            if busy:
                was_idle = not self._busy_engines and not self._active
                self._busy_engines.add(engine_id)
                if was_idle:
                    self._generation += 1
                    self._reset_phase_locked(self._clock())
            else:
                self._busy_engines.discard(engine_id)
                if not self._busy_engines and not self._active:
                    self._generation += 1
                    self._reset_phase_locked(self._clock())
                    self._wake_all_locked()

    def _reset_phase_locked(self, now: float) -> None:
        self._phase = "work"
        self._phase_started = now
        self._work_deadline = (
            now + self._work_quantum_s * self._share if self._share < 1.0 else None
        )
        self._rest_deadline = None
        self._activity_intervals.clear()

    def _wake_one_locked(self, engine_id: str) -> None:
        waiter = self._waiters.get(engine_id)
        if waiter is not None:
            loop, event = waiter
            if not loop.is_closed():
                loop.call_soon_threadsafe(event.set)

    def _wake_all_locked(self) -> None:
        for engine_id in tuple(self._waiters):
            self._wake_one_locked(engine_id)

    def _merge_activity_locked(self) -> float:
        intervals = sorted(self._activity_intervals)
        if not intervals:
            return 0.0
        start, end = intervals[0]
        total = 0.0
        for next_start, next_end in intervals[1:]:
            if next_start <= end:
                end = max(end, next_end)
            else:
                total += max(0.0, end - start)
                start, end = next_start, next_end
        total += max(0.0, end - start)
        return total

    def _close_work_locked(self, now: float) -> None:
        self._phase = "drain"
        if not self._active:
            self._start_rest_locked(now)

    def _start_rest_locked(self, now: float) -> None:
        work_union = self._merge_activity_locked()
        self._work_union_s += work_union
        if work_union <= 0 or self._share >= 1.0:
            # No compute debt is carried from idle, rejected or maintenance calls.
            self._reset_phase_locked(now)
            return
        phase_started = self._phase_started
        elapsed = max(0.0, now - (phase_started if phase_started is not None else now))
        natural_idle = max(0.0, elapsed - work_union)
        rest = max(
            0.0,
            work_union * (1.0 - self._share) / self._share - natural_idle,
        )
        deferred_deadlines = [
            started + self._max_deferral_s for started in self._deferred_since.values()
        ]
        latest_allowed = (
            min(deferred_deadlines)
            if deferred_deadlines
            else now + self._max_deferral_s
        )
        self._rest_deadline = min(now + rest, latest_allowed)
        self._phase = "rest"
        if self._rest_deadline <= now:
            self._reset_phase_locked(now)

    def _advance_locked(self, now: float) -> None:
        if self._share >= 1.0:
            if self._phase != "work" or self._phase_started is not None:
                self._reset_phase_locked(now)
            return
        if self._phase == "work":
            if self._phase_started is None:
                self._phase_started = now
                self._work_deadline = now + self._work_quantum_s * self._share
            if self._work_deadline is not None and now >= self._work_deadline:
                self._close_work_locked(now)
        elif self._phase == "drain" and not self._active:
            self._start_rest_locked(now)
        elif (
            self._phase == "rest"
            and self._rest_deadline is not None
            and now >= self._rest_deadline
        ):
            self._generation += 1
            self._reset_phase_locked(now)

    def _admission_wait_locked(self, now: float) -> float | None:
        self._advance_locked(now)
        if self._share >= 1.0:
            return 0.0
        if self._phase == "work":
            return 0.0
        if self._phase == "drain":
            return None
        if self._rest_deadline is None:
            return 0.0
        return max(0.0, self._rest_deadline - now)

    async def acquire(
        self,
        engine_id: str,
        wake_event: asyncio.Event | None = None,
    ) -> PacingPermit | None | _PacingDisabled:
        """Wait asynchronously for a registered burst opportunity.

        A denied operation retains one deferral deadline across wakeups. When
        that deadline expires, it receives one forced permit even in REST so a
        slow drain cannot produce an unbounded request stall.
        """
        if self.full_speed_snapshot:
            return PACING_DISABLED
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with self._lock:
            self._engine_lifecycle.setdefault(engine_id, 0)
            wait_started = self._deferred_since.setdefault(engine_id, self._clock())
            self._waiters[engine_id] = (loop, event)
        try:
            while True:
                with self._lock:
                    now = self._clock()
                    if engine_id in self._cancelled_engines:
                        self._deferred_since.pop(engine_id, None)
                        return None
                    if self.full_speed_snapshot:
                        self._deferred_since.pop(engine_id, None)
                        return PACING_DISABLED
                    wait_started = self._deferred_since.setdefault(
                        engine_id, wait_started
                    )
                    wait = self._admission_wait_locked(now)
                    forced = (
                        engine_id in self._deferred_since
                        and now - wait_started >= self._max_deferral_s
                    )
                    if wait == 0 or forced:
                        if forced:
                            self._missed_targets += 1
                        permit = PacingPermit(
                            engine_id=engine_id,
                            generation=self._generation,
                            lifecycle=self._engine_lifecycle.get(engine_id, 0),
                            permit_id=next(self._permit_ids),
                            forced=forced,
                        )
                        self._active[permit.permit_id] = permit
                        self._waiters.pop(engine_id, None)
                        return permit
                    event.clear()
                    self._waiters[engine_id] = (loop, event)
                    delay = wait
                    if delay is None:
                        delay = max(0.001, self._max_deferral_s - (now - wait_started))
                    else:
                        delay = min(
                            delay,
                            max(0.001, self._max_deferral_s - (now - wait_started)),
                        )
                event_task = asyncio.create_task(event.wait())
                wake_task = (
                    asyncio.create_task(wake_event.wait())
                    if wake_event is not None
                    else None
                )
                wait_tasks = {event_task}
                if wake_task is not None:
                    wait_tasks.add(wake_task)
                try:
                    done, _pending = await asyncio.wait(
                        wait_tasks,
                        timeout=delay,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    unfinished = [task for task in wait_tasks if not task.done()]
                    for task in unfinished:
                        task.cancel()
                    if unfinished:
                        await asyncio.gather(*unfinished, return_exceptions=True)
                if wake_task is not None and wake_task in done:
                    if self.full_speed_snapshot:
                        return PACING_DISABLED
                    return None
        finally:
            with self._lock:
                current = self._waiters.get(engine_id)
                if current is not None and current == (loop, event):
                    self._waiters.pop(engine_id, None)

    def step_allowed(self, permit: PacingPermit) -> bool:
        """Nonblocking worker recheck between scheduler steps."""
        with self._lock:
            if permit.engine_id in self._cancelled_engines:
                return False
            if self._active.get(permit.permit_id) != permit:
                return False
            if permit.lifecycle != self._engine_lifecycle.get(permit.engine_id, 0):
                return False
            if self.full_speed_snapshot:
                return True
            if permit.generation != self._generation:
                return False
            if permit.forced:
                return permit.permit_id not in self._forced_started
            self._advance_locked(self._clock())
            allowed = self._share >= 1.0 or self._phase == "work"
            if not allowed:
                self._deferred_since.setdefault(permit.engine_id, self._clock())
            return allowed

    def begin_step(self, permit: PacingPermit) -> bool:
        """Atomically authorize one scheduler step and acknowledge progress."""
        with self._lock:
            if not self.step_allowed(permit):
                return False
            if permit.forced and not self.full_speed_snapshot:
                self._forced_started.add(permit.permit_id)
            self._deferred_since.pop(permit.engine_id, None)
            return True

    def complete(
        self,
        permit: PacingPermit,
        activity_intervals: list[tuple[float, float]],
    ) -> None:
        """Release only when the executor callable actually completes."""
        with self._lock:
            if self._active.get(permit.permit_id) != permit:
                return
            self._active.pop(permit.permit_id, None)
            self._forced_started.discard(permit.permit_id)
            if permit.generation == self._generation:
                self._activity_intervals.extend(
                    (float(start), float(end))
                    for start, end in activity_intervals
                    if end > start
                )
            now = self._clock()
            self._advance_locked(now)
            self._wake_all_locked()
