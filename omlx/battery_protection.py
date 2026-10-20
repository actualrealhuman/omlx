# SPDX-License-Identifier: Apache-2.0
"""Battery policy for participating batched text and vision generation.

This module has no hardware, event-loop or MLX dependencies. The service owns
observation and persistence; engines own safe execution boundaries. Shares
describe submission pacing, not measured electrical power.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass(frozen=True)
class BatteryProtectionSettings:
    enabled: bool = False
    throttle_start_percent: float = 75.0
    suspend_percent: float = 50.0
    resume_percent: float = 55.0
    minimum_share: float = 0.10
    outage_share: float = 0.25
    emergency_share: float = 0.10
    outage_grace_seconds: float = 60.0
    ac_stabilization_seconds: float = 10.0
    telemetry_stale_seconds: float = 5.0
    restoration_share_per_second: float = 0.01

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("battery protection enabled must be a boolean")
        for item in fields(self):
            if item.name == "enabled":
                continue
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{item.name} must be a finite number")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{item.name} must be a finite number")
        if (
            not 0
            <= self.suspend_percent
            < self.resume_percent
            < self.throttle_start_percent
            <= 100
        ):
            raise ValueError(
                "battery thresholds must satisfy 0 <= suspend < resume < throttle_start <= 100"
            )
        if not 0.1 <= self.emergency_share <= self.outage_share <= 1.0:
            raise ValueError(
                "battery emergency/outage shares must satisfy 0.1 <= emergency <= outage <= 1"
            )
        if not 0.1 <= self.minimum_share <= 1.0:
            raise ValueError("minimum_share must be between 0.1 and 1.0")
        for name in ("outage_grace_seconds", "ac_stabilization_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.telemetry_stale_seconds < 1:
            raise ValueError("telemetry_stale_seconds must be at least 1")
        if not 0 < self.restoration_share_per_second <= 1:
            raise ValueError("restoration_share_per_second must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def numeric_options(self) -> dict[str, float]:
        """Return the persisted policy options, excluding the opt-in flag."""
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "enabled"
        }

    @classmethod
    def from_options(
        cls, enabled: bool, options: dict[str, Any] | None = None
    ) -> BatteryProtectionSettings:
        """Build a complete validated config from a partial numeric option map."""
        if not isinstance(enabled, bool):
            raise ValueError("battery protection enabled must be a boolean")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise ValueError("battery_protection must be an object")
        known = {item.name for item in fields(cls)} - {"enabled"}
        unknown = set(options) - known
        if unknown:
            raise ValueError(
                f"unknown battery protection option(s): {', '.join(sorted(unknown))}"
            )
        defaults = cls(enabled=enabled).numeric_options()
        defaults.update(options)
        return cls(enabled=enabled, **defaults)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatteryProtectionSettings:
        if not isinstance(data, dict):
            raise ValueError("battery protection settings must be an object")
        enabled = data.get("enabled", False)
        options = {key: value for key, value in data.items() if key != "enabled"}
        return cls.from_options(enabled, options)


@dataclass(frozen=True)
class BatterySample:
    observed_at: float
    supported: bool = True
    battery_present: bool | None = None
    charge_percent: float | None = None
    external_power: bool | None = None
    charging: bool | None = None


@dataclass(frozen=True)
class BatteryRecoveryState:
    """Small durable state; no process-local timestamps survive a restart."""

    reserve_suspended: bool = False
    outage_active: bool = False
    battery_seen: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatteryRecoveryState:
        # Invalid durable state must not become permission to resume.
        if not isinstance(data, dict):
            raise ValueError("invalid battery recovery state")
        if any(not isinstance(data.get(item.name), bool) for item in fields(cls)):
            raise ValueError("invalid battery recovery state")
        return cls(**{item.name: data[item.name] for item in fields(cls)})


@dataclass(frozen=True)
class BatteryProtectionStatus:
    enabled: bool
    state: str
    suspended: bool
    configured_share: float
    target_share: float
    effective_share: float
    battery_percent: float | None
    external_power: bool | None
    charging: bool | None
    sample_age_seconds: float | None
    outage_seconds: float | None
    ac_stable_seconds: float | None
    restoring: bool
    scope: str = "batched_text_vlm"


class BatteryPolicy:
    """Deterministic policy evaluated with explicit samples and monotonic time."""

    def __init__(
        self,
        settings: BatteryProtectionSettings | None = None,
        recovery: BatteryRecoveryState | None = None,
    ) -> None:
        self.settings = settings or BatteryProtectionSettings()
        recovery = recovery or BatteryRecoveryState()
        self._reserve = recovery.reserve_suspended
        self._outage = recovery.outage_active
        self._seen = recovery.battery_seen
        self._external_seen = False
        self._outage_started: float | None = None
        self._ac_since: float | None = None
        self._last_now: float | None = None
        self._effective: float | None = None
        self._was_suspended = False

    @property
    def recovery(self) -> BatteryRecoveryState:
        return BatteryRecoveryState(self._reserve, self._outage, self._seen)

    def inherit_system_state(self, source: "BatteryPolicy") -> None:
        """Share reserve/outage history while retaining this model's recovery ramp."""
        for name in (
            "_reserve",
            "_outage",
            "_seen",
            "_external_seen",
            "_outage_started",
            "_ac_since",
        ):
            setattr(self, name, getattr(source, name))

    @staticmethod
    def _percentage(value: float | None) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) and 0 <= value <= 100 else None

    def evaluate(
        self, sample: BatterySample | None, requested_share: float, *, now: float
    ) -> BatteryProtectionStatus:
        if (
            isinstance(requested_share, bool)
            or not isinstance(requested_share, (int, float))
            or not math.isfinite(requested_share)
            or not 0.0 <= requested_share <= 1
        ):
            raise ValueError("requested share must be between 0 and 1")
        if not math.isfinite(now) or (
            self._last_now is not None and now < self._last_now
        ):
            raise ValueError("policy time must be finite and monotonic")
        elapsed = 0.0 if self._last_now is None else now - self._last_now
        self._last_now = now
        cfg = self.settings
        age = None if sample is None else now - sample.observed_at
        fresh = (
            age is not None
            and math.isfinite(age)
            and 0 <= age <= cfg.telemetry_stale_seconds
        )
        percent = self._percentage(sample.charge_percent) if sample and fresh else None
        external = sample.external_power if sample and fresh else None
        charging = sample.charging if sample and fresh else None

        if sample and fresh and sample.battery_present is True:
            self._seen = True
            if percent is not None:
                if percent <= cfg.suspend_percent:
                    self._reserve = True
                elif percent >= cfg.resume_percent:
                    self._reserve = False

        # Track interruptions while monitoring, even if policy is disabled.
        if sample and fresh and sample.battery_present is True:
            if external is False:
                if not self._outage:
                    self._outage_started = now if self._external_seen else None
                self._outage = True
                self._ac_since = None
            elif external is True:
                self._external_seen = True
                if self._outage:
                    if self._ac_since is None:
                        self._ac_since = now
                    if now - self._ac_since >= cfg.ac_stabilization_seconds:
                        self._outage = False
                        self._outage_started = None
                        self._ac_since = None
            else:
                self._ac_since = None
        else:
            # Missing observations cannot qualify stable AC restoration.
            self._ac_since = None

        outage_age = (
            None if self._outage_started is None else now - self._outage_started
        )
        ac_age = None if self._ac_since is None else now - self._ac_since
        suspended = False
        target = float(requested_share)
        state = "normal"
        if not cfg.enabled:
            state = "disabled"
        elif sample and not sample.supported and not self._seen:
            state = "unsupported"
        elif sample and fresh and sample.battery_present is False and not self._seen:
            state = "no_battery"
        elif (
            not sample
            or not sample.supported
            or not fresh
            or sample.battery_present is not True
            or percent is None
            or external is None
        ):
            state, suspended = "telemetry_unavailable", True
        elif self._reserve:
            state, suspended = "reserve_suspended", True
        else:
            if percent < cfg.throttle_start_percent:
                floor = min(cfg.minimum_share, requested_share)
                target = floor + (requested_share - floor) * (
                    percent - cfg.suspend_percent
                ) / (cfg.throttle_start_percent - cfg.suspend_percent)
                state = "battery_throttled"
            if self._outage:
                extended = outage_age is None or outage_age >= cfg.outage_grace_seconds
                cap = cfg.emergency_share if extended else cfg.outage_share
                target = min(target, cap)
                state = (
                    "ac_stabilizing"
                    if self._ac_since is not None
                    else ("battery_emergency" if extended else "power_interruption")
                )

        target = 0.0 if suspended else min(requested_share, target)
        unrestricted = state in {"disabled", "unsupported", "no_battery"}
        if suspended:
            self._effective = 0.1
            effective = 0.0
        elif unrestricted or self._effective is None:
            self._effective = target
            effective = target
        else:
            if self._was_suspended:
                self._effective = min(cfg.minimum_share, target)
                elapsed = 0.0
            self._effective = min(
                target, self._effective + elapsed * cfg.restoration_share_per_second
            )
            effective = self._effective
        self._was_suspended = suspended
        return BatteryProtectionStatus(
            enabled=cfg.enabled,
            state=state,
            suspended=suspended,
            configured_share=requested_share,
            target_share=target,
            effective_share=effective,
            battery_percent=percent,
            external_power=external,
            charging=charging,
            sample_age_seconds=age if age is not None and math.isfinite(age) else None,
            outage_seconds=outage_age,
            ac_stable_seconds=ac_age,
            restoring=not suspended and effective < target - 1e-9,
        )
