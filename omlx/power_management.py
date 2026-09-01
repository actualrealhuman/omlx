# SPDX-License-Identifier: Apache-2.0
"""Process-wide battery policy and cooperative inference pause gate."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .utils.power_sources import (
    PowerSourceChangeNotifier,
    PowerSourceReader,
    PowerSourceSnapshot,
)

logger = logging.getLogger(__name__)

PowerManagerState = Literal[
    "disabled",
    "unsupported",
    "normal",
    "paused_on_battery",
    "ac_stabilization",
    "charge_recovery",
    "stale_telemetry",
]


class InferencePauseGate:
    """A process-wide gate readable from both asyncio and MLX threads.

    Policy changes occur on the server event loop. MLX executor threads only
    call :meth:`is_paused`, which is deliberately synchronous and cheap.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused = False
        self._reason: str | None = None
        self._event: asyncio.Event | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_paused(self, paused: bool, reason: str | None = None) -> bool:
        """Set gate state and wake async waiters when inference may resume.

        Returns ``True`` only when the paused state changed.
        """

        with self._lock:
            changed = self._paused != paused
            self._paused = paused
            self._reason = reason if paused else None
            event = self._event
            loop = self._event_loop

        if event is not None and loop is not None and not loop.is_closed():
            callback = event.clear if paused else event.set
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                callback()
            else:
                loop.call_soon_threadsafe(callback)
        return changed

    async def wait_until_resumed(self) -> None:
        """Return immediately when open; otherwise wait without busy polling."""

        while True:
            loop = asyncio.get_running_loop()
            with self._lock:
                if not self._paused:
                    return
                if self._event is None or self._event_loop is not loop:
                    self._event = asyncio.Event()
                    self._event_loop = loop
                event = self._event
                event.clear()
            await event.wait()


_PROCESS_INFERENCE_GATE = InferencePauseGate()


def get_process_inference_gate() -> InferencePauseGate:
    return _PROCESS_INFERENCE_GATE


@dataclass(frozen=True)
class PowerManagerStatus:
    state: PowerManagerState
    paused: bool
    pause_reason: str | None
    state_since_monotonic_ns: int
    last_sample: dict[str, Any] | None
    sample_age_seconds: float | None
    source_change_latency_seconds: float | None


class BatteryPowerManager:
    """Monitor effective power source and operate the inference gate.

    This first control layer implements the non-negotiable safety behavior:
    effective battery power pauses immediately and AC restoration is held for
    a configurable settling interval. Charge-floor duty control builds on the
    same gate and telemetry stream.
    """

    def __init__(
        self,
        settings: Any,
        *,
        reader: PowerSourceReader | None = None,
        notifier: PowerSourceChangeNotifier | None = None,
        gate: InferencePauseGate | None = None,
    ) -> None:
        self.settings = settings
        self.reader = reader or PowerSourceReader()
        self.notifier = notifier or PowerSourceChangeNotifier()
        self.gate = gate or get_process_inference_gate()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._state: PowerManagerState = "disabled"
        self._state_since_ns = time.monotonic_ns()
        self._last_snapshot: PowerSourceSnapshot | None = None
        self._last_sample_completed_ns: int | None = None
        self._last_source: str | None = None
        self._ac_stable_after_ns: int | None = None
        self._seen_internal_battery = False
        self._source_change_detected_ns: int | None = None
        self._source_change_latency_seconds: float | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # Establish the gate synchronously before lifespan startup can yield
        # an HTTP port or pinned-model preload task to the event loop.
        try:
            self.apply_snapshot(self.reader.sample(), now_ns=time.monotonic_ns())
        except Exception:  # noqa: BLE001 - unsupported telemetry is non-fatal
            logger.exception("Initial battery power sample failed")
        self._running = True
        self._task = asyncio.create_task(
            self._run(), name="battery-power-manager"
        )

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.notifier.close()
        self.reader.close()
        self.gate.set_paused(False)

    def apply_settings(self, settings: Any) -> None:
        """Hot-apply a replacement settings object."""

        self.settings = settings
        if self._last_snapshot is not None:
            self.apply_snapshot(self._last_snapshot)
        elif not bool(settings.enabled):
            self._set_state("disabled", paused=False)

    def status(self, now_ns: int | None = None) -> PowerManagerStatus:
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        age = None
        if self._last_sample_completed_ns is not None:
            age = max(0.0, (now_ns - self._last_sample_completed_ns) / 1e9)
        return PowerManagerStatus(
            state=self._state,
            paused=self.gate.is_paused(),
            pause_reason=self.gate.reason,
            state_since_monotonic_ns=self._state_since_ns,
            last_sample=(
                asdict(self._last_snapshot) if self._last_snapshot is not None else None
            ),
            sample_age_seconds=age,
            source_change_latency_seconds=self._source_change_latency_seconds,
        )

    def _set_state(
        self,
        state: PowerManagerState,
        *,
        paused: bool,
        reason: str | None = None,
        now_ns: int | None = None,
    ) -> None:
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        state_changed = state != self._state
        gate_changed = self.gate.set_paused(paused, reason)
        if state_changed:
            self._state = state
            self._state_since_ns = now_ns
        if state_changed or gate_changed:
            logger.info(
                "Battery power policy: state=%s inference=%s%s",
                state,
                "paused" if paused else "enabled",
                f" reason={reason}" if reason else "",
            )

    def apply_snapshot(
        self,
        snapshot: PowerSourceSnapshot,
        *,
        now_ns: int | None = None,
    ) -> None:
        """Apply one reading. Kept deterministic for policy tests."""

        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        previous_source = self._last_source
        self._last_snapshot = snapshot
        self._last_sample_completed_ns = now_ns
        self._last_source = snapshot.source
        self._seen_internal_battery |= snapshot.battery_present

        if self._source_change_detected_ns is not None:
            self._source_change_latency_seconds = max(
                0.0, (now_ns - self._source_change_detected_ns) / 1e9
            )
            self._source_change_detected_ns = None

        if not bool(self.settings.enabled):
            self._ac_stable_after_ns = None
            self._set_state("disabled", paused=False, now_ns=now_ns)
            return

        if snapshot.source == "battery":
            self._ac_stable_after_ns = None
            self._set_state(
                "paused_on_battery",
                paused=True,
                reason="effective power source is the internal battery",
                now_ns=now_ns,
            )
            return

        if snapshot.source == "ac":
            if previous_source == "battery":
                delay_ns = max(
                    0,
                    int(float(self.settings.ac_stabilization_seconds) * 1e9),
                )
                self._ac_stable_after_ns = now_ns + delay_ns
            if (
                self._ac_stable_after_ns is not None
                and now_ns < self._ac_stable_after_ns
            ):
                self._set_state(
                    "ac_stabilization",
                    paused=True,
                    reason="waiting for AC power to stabilize",
                    now_ns=now_ns,
                )
                return
            self._ac_stable_after_ns = None
            self._set_state("normal", paused=False, now_ns=now_ns)
            return

        # A desktop, UPS-only host, or a platform without IOPowerSources is not
        # governed. Once a real internal battery has been observed, however,
        # losing the effective-source signal is fail-safe until telemetry
        # recovers.
        if self._seen_internal_battery:
            self._set_state(
                "stale_telemetry",
                paused=True,
                reason="battery power telemetry is unavailable",
                now_ns=now_ns,
            )
        else:
            self._set_state("unsupported", paused=False, now_ns=now_ns)

    async def _run(self) -> None:
        next_sample = 0.0
        while self._running:
            try:
                now = time.monotonic()
                notified = self.notifier.changed()
                if notified:
                    self._source_change_detected_ns = time.monotonic_ns()
                if notified or now >= next_sample:
                    snapshot = self.reader.sample()
                    completed_ns = time.monotonic_ns()
                    self.apply_snapshot(snapshot, now_ns=completed_ns)
                    next_sample = now + max(
                        0.01, float(self.settings.sample_interval_seconds)
                    )
                elif (
                    self._ac_stable_after_ns is not None
                    and time.monotonic_ns() >= self._ac_stable_after_ns
                    and self._last_snapshot is not None
                ):
                    self.apply_snapshot(
                        self._last_snapshot, now_ns=time.monotonic_ns()
                    )

                await asyncio.sleep(
                    max(
                        0.005,
                        min(
                            float(self.settings.notification_poll_interval_seconds),
                            float(self.settings.sample_interval_seconds),
                        ),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - telemetry must not kill the server
                logger.exception("Battery power telemetry loop failed")
                if self._seen_internal_battery:
                    self._set_state(
                        "stale_telemetry",
                        paused=True,
                        reason="battery power telemetry loop failed",
                    )
                await asyncio.sleep(0.25)
