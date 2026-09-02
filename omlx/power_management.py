# SPDX-License-Identifier: Apache-2.0
"""Process-wide battery policy and cooperative inference pause gate."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
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
        self._condition = threading.Condition(self._lock)
        self._paused = False
        self._reason: str | None = None
        self._event: asyncio.Event | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._max_work_quantum_seconds: float | None = None
        self._pause_requested_ns: int | None = None
        self._last_pause_response_seconds: float | None = None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def max_work_quantum_seconds(self) -> float | None:
        with self._lock:
            return self._max_work_quantum_seconds

    @property
    def last_pause_response_seconds(self) -> float | None:
        with self._lock:
            return self._last_pause_response_seconds

    def set_max_work_quantum_seconds(self, seconds: float | None) -> None:
        with self._lock:
            self._max_work_quantum_seconds = (
                None if seconds is None else max(0.001, float(seconds))
            )

    def acknowledge_pause_boundary(self, *, now_ns: int | None = None) -> None:
        """Record when an inference thread first observes a closed gate."""

        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        with self._lock:
            requested_ns = self._pause_requested_ns
            if self._paused and requested_ns is not None:
                self._last_pause_response_seconds = max(
                    0.0, (now_ns - requested_ns) / 1e9
                )
                self._pause_requested_ns = None

    def set_paused(self, paused: bool, reason: str | None = None) -> bool:
        """Set gate state and wake async waiters when inference may resume.

        Returns ``True`` only when the paused state changed.
        """

        with self._condition:
            changed = self._paused != paused
            self._paused = paused
            self._reason = reason if paused else None
            if changed and paused:
                self._pause_requested_ns = time.monotonic_ns()
            elif not paused:
                self._pause_requested_ns = None
            event = self._event
            loop = self._event_loop
            if changed:
                self._condition.notify_all()

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
                if self._pause_requested_ns is not None:
                    self._last_pause_response_seconds = max(
                        0.0,
                        (time.monotonic_ns() - self._pause_requested_ns) / 1e9,
                    )
                    self._pause_requested_ns = None
                if self._event is None or self._event_loop is not loop:
                    self._event = asyncio.Event()
                    self._event_loop = loop
                event = self._event
                event.clear()
            await event.wait()

    def wait_until_resumed_sync(
        self, stop_event: threading.Event | None = None
    ) -> bool:
        """Block an inference worker at a boundary until the gate reopens.

        Returns ``False`` when a caller-provided cancellation event fires.
        The short timed wait is used only when cancellation must also wake a
        worker; normal power transitions notify the condition immediately.
        """

        with self._condition:
            acknowledged = False
            while self._paused:
                if not acknowledged and self._pause_requested_ns is not None:
                    self._last_pause_response_seconds = max(
                        0.0,
                        (time.monotonic_ns() - self._pause_requested_ns) / 1e9,
                    )
                    self._pause_requested_ns = None
                    acknowledged = True
                if stop_event is not None and stop_event.is_set():
                    return False
                self._condition.wait(timeout=0.1 if stop_event is not None else None)
            return True


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
    filtered_charge_watts: float | None
    effective_charge_deadband_watts: float | None
    inference_duty: float
    idle_charge_baseline_watts: float | None
    inferred_inference_watts_at_full_duty: float | None
    last_pause_response_seconds: float | None


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
        self._apply_gate_settings()
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
        self._filtered_charge_watts: float | None = None
        self._last_filter_ns: int | None = None
        self._last_charge_measurement_ns: int | None = None
        self._charge_samples: deque[float] = deque(maxlen=64)
        self._effective_deadband_watts: float | None = None
        self._recovery_active = False
        self._inference_duty = 1.0
        self._duty_cycle_anchor_ns = self._state_since_ns
        self._idle_charge_baseline_watts: float | None = None
        self._inferred_full_duty_watts: float | None = None
        self._below_target_since_ns: int | None = None
        self._above_target_since_ns: int | None = None
        self._next_probe_ns = self._state_since_ns

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
        self.gate.set_max_work_quantum_seconds(None)

    def apply_settings(self, settings: Any) -> None:
        """Hot-apply a replacement settings object."""

        self.settings = settings
        self._apply_gate_settings()
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
            filtered_charge_watts=self._filtered_charge_watts,
            effective_charge_deadband_watts=self._effective_deadband_watts,
            inference_duty=self._inference_duty,
            idle_charge_baseline_watts=self._idle_charge_baseline_watts,
            inferred_inference_watts_at_full_duty=self._inferred_full_duty_watts,
            last_pause_response_seconds=self.gate.last_pause_response_seconds,
        )

    def _apply_gate_settings(self) -> None:
        self.gate.set_max_work_quantum_seconds(
            float(getattr(self.settings, "max_cooperative_pause_latency_seconds", 0.25))
            if bool(getattr(self.settings, "enabled", False))
            else None
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
        if state_changed or (gate_changed and state != "charge_recovery"):
            logger.info(
                "Battery power policy: state=%s inference=%s%s",
                state,
                "paused" if paused else "enabled",
                f" reason={reason}" if reason else "",
            )
        elif gate_changed:
            logger.debug(
                "Charge recovery duty gate %s (duty=%.3f)",
                "paused" if paused else "open",
                self._inference_duty,
            )

    def _reset_charge_recovery(self, *, now_ns: int, duty: float = 1.0) -> None:
        self._recovery_active = False
        self._inference_duty = duty
        self._duty_cycle_anchor_ns = now_ns
        self._below_target_since_ns = None
        self._above_target_since_ns = None
        self._next_probe_ns = now_ns

    def _update_charge_measurement(
        self, watts: float | None, *, now_ns: int
    ) -> None:
        if watts is None or not math.isfinite(watts):
            return
        self._last_charge_measurement_ns = now_ns
        previous_ns = self._last_filter_ns
        self._last_filter_ns = now_ns
        self._charge_samples.append(watts)

        tau = max(0.001, float(self.settings.charge_filter_seconds))
        if self._filtered_charge_watts is None or previous_ns is None:
            self._filtered_charge_watts = watts
        else:
            elapsed = max(0.0, (now_ns - previous_ns) / 1e9)
            alpha = min(1.0, elapsed / (tau + elapsed))
            self._filtered_charge_watts += alpha * (
                watts - self._filtered_charge_watts
            )

        configured_deadband = self.settings.charge_deadband_watts
        if configured_deadband is not None:
            self._effective_deadband_watts = max(0.0, float(configured_deadband))
        else:
            samples = tuple(self._charge_samples)
            noise = 0.0
            if len(samples) >= 3:
                mean = sum(samples) / len(samples)
                variance = sum((sample - mean) ** 2 for sample in samples) / len(
                    samples
                )
                noise = 2.0 * math.sqrt(max(0.0, variance))
            self._effective_deadband_watts = min(
                float(self.settings.charge_deadband_max_watts),
                max(float(self.settings.charge_deadband_min_watts), noise),
            )

        filtered = self._filtered_charge_watts
        if filtered is None:
            return
        if self._inference_duty <= 0.0:
            if self._idle_charge_baseline_watts is None:
                self._idle_charge_baseline_watts = filtered
            else:
                self._idle_charge_baseline_watts += 0.2 * (
                    filtered - self._idle_charge_baseline_watts
                )
        elif self._idle_charge_baseline_watts is not None:
            inferred = max(
                0.0,
                (self._idle_charge_baseline_watts - filtered)
                / max(0.01, self._inference_duty),
            )
            if inferred > 0.0:
                if self._inferred_full_duty_watts is None:
                    self._inferred_full_duty_watts = inferred
                else:
                    self._inferred_full_duty_watts += 0.1 * (
                        inferred - self._inferred_full_duty_watts
                    )

    def _update_recovery_duty(self, *, now_ns: int) -> None:
        charge_watts = self._filtered_charge_watts
        measurement_age = (
            math.inf
            if self._last_charge_measurement_ns is None
            else (now_ns - self._last_charge_measurement_ns) / 1e9
        )
        if charge_watts is None or measurement_age > float(
            self.settings.telemetry_stale_seconds
        ):
            self._inference_duty = 0.0
            return

        target = float(self.settings.target_charge_watts)
        deadband = float(self._effective_deadband_watts or 0.0)
        if charge_watts < target - deadband:
            self._above_target_since_ns = None
            if self._below_target_since_ns is None:
                self._below_target_since_ns = now_ns
            confirmed = (now_ns - self._below_target_since_ns) / 1e9 >= float(
                self.settings.reduction_confirmation_seconds
            )
            if confirmed:
                previous = self._inference_duty
                self._inference_duty = max(
                    0.0,
                    previous - float(self.settings.duty_reduction_step),
                )
                self._below_target_since_ns = now_ns
                if self._inference_duty <= 0.0:
                    self._next_probe_ns = now_ns + int(
                        float(self.settings.paused_probe_interval_seconds) * 1e9
                    )
                if self._inference_duty != previous:
                    self._duty_cycle_anchor_ns = now_ns
            return

        self._below_target_since_ns = None
        if charge_watts <= target + deadband:
            self._above_target_since_ns = None
            return

        if self._above_target_since_ns is None:
            self._above_target_since_ns = now_ns
        confirmed = (now_ns - self._above_target_since_ns) / 1e9 >= float(
            self.settings.restoration_confirmation_seconds
        )
        if not confirmed:
            return

        previous = self._inference_duty
        if previous <= 0.0:
            if now_ns < self._next_probe_ns:
                return
            candidate = float(self.settings.paused_probe_duty)
            self._next_probe_ns = now_ns + int(
                float(self.settings.paused_probe_interval_seconds) * 1e9
            )
        else:
            candidate = previous + float(self.settings.duty_restoration_step)
            baseline = self._idle_charge_baseline_watts
            full_duty_watts = self._inferred_full_duty_watts
            if baseline is not None and full_duty_watts is not None:
                sustainable = max(
                    0.0,
                    min(1.0, (baseline - target - deadband) / full_duty_watts),
                )
                candidate = min(candidate, max(previous, sustainable))
        self._inference_duty = min(1.0, max(0.0, candidate))
        self._above_target_since_ns = now_ns
        if self._inference_duty != previous:
            self._duty_cycle_anchor_ns = now_ns

    def _apply_charge_recovery_gate(self, *, now_ns: int) -> None:
        duty = min(1.0, max(0.0, self._inference_duty))
        if duty <= 0.0:
            paused = True
        elif duty >= 1.0:
            paused = False
        else:
            period_ns = max(
                1,
                int(float(self.settings.duty_cycle_period_seconds) * 1e9),
            )
            phase_ns = (now_ns - self._duty_cycle_anchor_ns) % period_ns
            paused = phase_ns >= int(period_ns * duty)
        self._set_state(
            "charge_recovery",
            paused=paused,
            reason=(
                "preserving the configured net battery charging rate"
                if paused
                else None
            ),
            now_ns=now_ns,
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
        if snapshot.source == "ac" and previous_source != "ac":
            self._filtered_charge_watts = None
            self._last_filter_ns = None
            self._last_charge_measurement_ns = None
            self._charge_samples.clear()
            self._effective_deadband_watts = None
            self._idle_charge_baseline_watts = None
            self._inferred_full_duty_watts = None
        self._last_snapshot = snapshot
        self._last_sample_completed_ns = now_ns
        self._last_source = snapshot.source
        self._seen_internal_battery |= snapshot.battery_present
        if snapshot.source == "ac":
            self._update_charge_measurement(
                snapshot.battery_charge_watts, now_ns=now_ns
            )

        if self._source_change_detected_ns is not None:
            self._source_change_latency_seconds = max(
                0.0, (now_ns - self._source_change_detected_ns) / 1e9
            )
            self._source_change_detected_ns = None

        if not bool(self.settings.enabled):
            self._ac_stable_after_ns = None
            self._reset_charge_recovery(now_ns=now_ns)
            self._set_state("disabled", paused=False, now_ns=now_ns)
            return

        if snapshot.source == "battery":
            self._ac_stable_after_ns = None
            self._reset_charge_recovery(now_ns=now_ns, duty=0.0)
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
            charge_percent = snapshot.charge_percent
            floor = float(self.settings.charge_floor_percent)
            recovery_threshold = min(
                100.0,
                floor + float(self.settings.recovery_hysteresis_percent),
            )
            if self._recovery_active:
                if charge_percent is not None and charge_percent >= recovery_threshold:
                    self._reset_charge_recovery(now_ns=now_ns)
                else:
                    self._update_recovery_duty(now_ns=now_ns)
                    self._apply_charge_recovery_gate(now_ns=now_ns)
                    return
            elif charge_percent is not None and charge_percent <= floor:
                self._recovery_active = True
                self._inference_duty = 0.0
                self._duty_cycle_anchor_ns = now_ns
                self._below_target_since_ns = None
                self._above_target_since_ns = None
                self._next_probe_ns = now_ns
                self._update_recovery_duty(now_ns=now_ns)
                self._apply_charge_recovery_gate(now_ns=now_ns)
                return

            self._set_state("normal", paused=False, now_ns=now_ns)
            return

        # A desktop, UPS-only host, or a platform without IOPowerSources is not
        # governed. Once a real internal battery has been observed, however,
        # losing the effective-source signal is fail-safe until telemetry
        # recovers.
        if self._seen_internal_battery:
            self._inference_duty = 0.0
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
                elif self._state == "charge_recovery":
                    self._apply_charge_recovery_gate(now_ns=time.monotonic_ns())

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
