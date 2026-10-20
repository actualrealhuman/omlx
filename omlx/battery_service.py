# SPDX-License-Identifier: Apache-2.0
"""Observe host battery state and apply the batched-generation policy."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import time
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable

from .battery_protection import (
    BatteryPolicy,
    BatteryProtectionSettings,
    BatteryProtectionStatus,
    BatteryRecoveryState,
    BatterySample,
)
from .generation_gate import GenerationGate, get_generation_gate
from .utils.power_sources import (
    PowerSourceChangeNotifier,
    PowerSourceReader,
    PowerSourceSnapshot,
)

logger = logging.getLogger(__name__)


class _UnavailablePowerMonitor:
    def sample(self):
        return PowerSourceSnapshot(time.monotonic_ns(), "unknown", False, False)

    def changed(self):
        return False

    def close(self):
        pass


def _monitor(factory):
    try:
        return factory()
    except Exception:
        logger.warning("Power monitor initialization failed", exc_info=True)
        return _UnavailablePowerMonitor()


class BatteryProtectionService:

    def __init__(
        self,
        *,
        enabled: bool,
        policy_options: dict | None = None,
        requested_share: float,
        apply_share: Callable[[float], None],
        state_path: Path,
        reader=None,
        notifier=None,
        gate: GenerationGate | None = None,
        clock: Callable[[], float] = time.monotonic,
        apply_model_share: Callable[[str, float], None] | None = None,
        apply_suspension: Callable[[bool], None] | None = None,
    ) -> None:
        self.reader = reader if reader is not None else _monitor(PowerSourceReader)
        self.notifier = (
            notifier if notifier is not None else _monitor(PowerSourceChangeNotifier)
        )
        self.gate = gate or get_generation_gate()
        self._clock = clock
        self._state_path = state_path
        self._recovery = self._load_recovery()
        self.policy = BatteryPolicy(
            BatteryProtectionSettings.from_options(enabled, policy_options),
            self._recovery,
        )
        self._requested_share = requested_share
        self._apply_share = apply_share
        self._applied_share: float | None = None
        self._sample: BatterySample | None = None
        self._status: BatteryProtectionStatus | None = None
        self._task: asyncio.Task | None = None
        self._next_sample = 0.0
        self._persistence_warned = False
        self._model_policies: dict[str, BatteryPolicy] = {}
        self._model_shares: dict[str, float | None] = {}
        self._model_effective: dict[str, float] = {}
        self._apply_model_share = apply_model_share
        self._apply_suspension = apply_suspension

    def _load_recovery(self) -> BatteryRecoveryState:
        try:
            return BatteryRecoveryState.from_dict(
                json.loads(self._state_path.read_text())
            )
        except FileNotFoundError:
            return BatteryRecoveryState()
        except (OSError, ValueError, TypeError):
            logger.warning(
                "Battery recovery state unavailable; require reserve recovery"
            )
            return BatteryRecoveryState(reserve_suspended=True, battery_seen=True)

    def _save_recovery(self) -> None:
        if not self.policy.settings.enabled and not self._state_path.exists():
            return
        recovery = self.policy.recovery
        if recovery == self._recovery:
            return
        temporary = self._state_path.with_suffix(".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                json.dump(asdict(recovery), handle)
            os.replace(temporary, self._state_path)
            self._recovery = recovery
            self._persistence_warned = False
        except OSError:
            if not self._persistence_warned:
                logger.warning(
                    "Could not persist battery recovery state", exc_info=True
                )
                self._persistence_warned = True

    def _read_sample(self, now: float) -> BatterySample:
        raw = self.reader.sample()
        if sys.platform != "darwin":
            return BatterySample(now, supported=False)
        if raw.source == "unknown":
            return BatterySample(now)
        external = (
            True
            if raw.adapter_present or raw.source == "ac"
            else (False if raw.source == "battery" else None)
        )
        return BatterySample(
            now,
            battery_present=raw.battery_present,
            charge_percent=raw.charge_percent,
            external_power=external,
            charging=raw.charging,
        )

    def sample_now(self) -> None:
        now = self._clock()
        try:
            self._sample = self._read_sample(now)
        except Exception:
            # Last successful observation ages normally; failures cannot refresh it.
            logger.warning("Battery observation failed", exc_info=True)
        self._next_sample = now + 1.0
        self._evaluate(now)

    def _evaluate(self, now: float) -> None:
        status = self.policy.evaluate(self._sample, self._requested_share, now=now)
        share = status.effective_share
        # Close the hard gate before touching pacing; suspension must never
        # depend on pacing deadlines.
        restricted = status.enabled and (
            status.state not in {"normal", "disabled", "unsupported", "no_battery"}
            or status.restoring
        )
        if status.suspended:
            if self._apply_suspension is not None:
                self._apply_suspension(True)
            self.gate.set_state(paused=True, restricted=True, reason=status.state)
        if not status.suspended and share != self._applied_share:
            self._apply_share(share)
            self._applied_share = share
        if not status.suspended:
            self.gate.set_state(
                paused=False, restricted=restricted, reason=status.state
            )
        self._status = replace(
            status, effective_share=0.0 if status.suspended else share
        )
        for model_id, policy in self._model_policies.items():
            policy.settings = self.policy.settings
            policy.inherit_system_state(self.policy)
            configured = self._model_shares[model_id]
            maximum = min(
                self._requested_share, 1.0 if configured is None else configured
            )
            model_status = policy.evaluate(self._sample, maximum, now=now)
            effective = (
                0.0 if status.suspended else min(share, model_status.effective_share)
            )
            self._model_effective[model_id] = effective
            if self._apply_model_share is not None:
                self._apply_model_share(model_id, effective)
        if not status.suspended and self._apply_suspension is not None:
            self._apply_suspension(False)
        self._save_recovery()

    def configure_model_share(self, model_id: str, share: float | None) -> None:
        if share is not None and (
            isinstance(share, bool)
            or not isinstance(share, (int, float))
            or not math.isfinite(share)
            or not 0.0 <= share <= 1.0
        ):
            raise ValueError("model share must be null or between 0 and 1")
        self._model_shares[model_id] = share
        if model_id not in self._model_policies:
            self._model_policies[model_id] = BatteryPolicy(
                self.policy.settings, self.policy.recovery
            )
        self._evaluate(self._clock())

    def model_effective_share(self, model_id: str) -> float:
        if self.gate.paused_snapshot:
            return 0.0
        return self._model_effective.get(model_id, self._requested_share)

    def set_requested_share(self, share: float) -> None:
        if (
            isinstance(share, bool)
            or not isinstance(share, (int, float))
            or not math.isfinite(share)
            or not 0.0 <= share <= 1
        ):
            raise ValueError("requested share must be between 0 and 1")
        self._requested_share = share
        self._evaluate(self._clock())

    def set_enabled(self, enabled: bool) -> None:
        self.configure(
            BatteryProtectionSettings.from_options(
                enabled, self.policy.settings.numeric_options()
            )
        )

    def configure(
        self,
        settings: BatteryProtectionSettings,
        requested_share: float | None = None,
    ) -> None:
        """Atomically replace validated policy options and immediately reevaluate."""
        if not isinstance(settings, BatteryProtectionSettings):
            raise ValueError("settings must be validated battery protection settings")
        if requested_share is not None:
            if (
                isinstance(requested_share, bool)
                or not isinstance(requested_share, (int, float))
                or not math.isfinite(requested_share)
                or not 0.0 <= requested_share <= 1
            ):
                raise ValueError("requested share must be between 0 and 1")
        self.policy.settings = settings
        if requested_share is not None:
            self._requested_share = float(requested_share)
        self._evaluate(self._clock())

    def status(self) -> dict:
        result = asdict(self._status) if self._status is not None else {}
        result["policy"] = self.policy.settings.to_dict()
        return result

    def start(self) -> None:
        if self._task is not None:
            return
        self.sample_now()  # before startup can yield to pinned preload/admission
        self._task = asyncio.create_task(self._run(), name="battery-protection")

    async def _run(self) -> None:
        while True:
            try:
                if self.notifier.changed() or self._clock() >= self._next_sample:
                    self.sample_now()
                else:
                    self._evaluate(self._clock())
            except Exception:
                logger.exception("Battery protection monitor failed")
                if self.policy.settings.enabled:
                    if self._apply_suspension is not None:
                        self._apply_suspension(True)
                    self.gate.set_state(
                        paused=True, restricted=True, reason="telemetry_unavailable"
                    )
                    if self._status is not None:
                        self._status = replace(
                            self._status,
                            state="telemetry_unavailable",
                            suspended=True,
                            target_share=0.0,
                            effective_share=0.0,
                            restoring=False,
                        )
            await asyncio.sleep(0.25)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.notifier.close()
        self.reader.close()
        self.gate.set_state(paused=False, restricted=False)
