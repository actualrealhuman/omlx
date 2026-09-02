# SPDX-License-Identifier: Apache-2.0
"""Tests for battery policy and the cooperative inference gate."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from omlx.power_management import BatteryPowerManager, InferencePauseGate
from omlx.utils.power_sources import PowerSourceSnapshot


class _Closer:
    available = False

    def close(self):
        pass


def _settings(**overrides):
    values = {
        "enabled": True,
        "ac_stabilization_seconds": 8.0,
        "sample_interval_seconds": 0.25,
        "notification_poll_interval_seconds": 0.05,
        "telemetry_stale_seconds": 2.0,
        "charge_floor_percent": 50.0,
        "recovery_hysteresis_percent": 2.0,
        "target_charge_watts": 10.0,
        "charge_filter_seconds": 0.01,
        "charge_deadband_watts": 1.0,
        "charge_deadband_min_watts": 1.0,
        "charge_deadband_max_watts": 5.0,
        "reduction_confirmation_seconds": 0.5,
        "restoration_confirmation_seconds": 3.0,
        "duty_reduction_step": 0.20,
        "duty_restoration_step": 0.05,
        "duty_cycle_period_seconds": 2.0,
        "paused_probe_duty": 0.05,
        "paused_probe_interval_seconds": 10.0,
        "max_cooperative_pause_latency_seconds": 0.25,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot(
    source,
    *,
    battery_present=True,
    charge_percent=80.0,
    charge_watts=0.0,
):
    return PowerSourceSnapshot(
        monotonic_ns=0,
        source=source,
        battery_present=battery_present,
        adapter_present=source == "ac",
        charge_percent=charge_percent,
        battery_charge_watts=charge_watts,
    )


def _manager(settings=None):
    return BatteryPowerManager(
        settings or _settings(),
        reader=_Closer(),
        notifier=_Closer(),
        gate=InferencePauseGate(),
    )


def test_starting_on_battery_closes_gate_immediately():
    manager = _manager()

    manager.apply_snapshot(_snapshot("battery"), now_ns=1_000)

    assert manager.gate.is_paused()
    assert manager.status(now_ns=1_000).state == "paused_on_battery"


def test_ac_restoration_remains_paused_for_configured_interval():
    manager = _manager(_settings(ac_stabilization_seconds=8.0))
    manager.apply_snapshot(_snapshot("battery"), now_ns=1_000)

    manager.apply_snapshot(_snapshot("ac"), now_ns=2_000)
    assert manager.status(now_ns=2_000).state == "ac_stabilization"
    assert manager.gate.is_paused()

    manager.apply_snapshot(_snapshot("ac"), now_ns=8_000_002_000)
    assert manager.status(now_ns=8_000_002_000).state == "normal"
    assert not manager.gate.is_paused()


def test_unknown_source_is_fail_safe_after_a_battery_was_observed():
    manager = _manager()
    manager.apply_snapshot(_snapshot("ac"), now_ns=1_000)

    manager.apply_snapshot(_snapshot("unknown"), now_ns=2_000)

    assert manager.status(now_ns=2_000).state == "stale_telemetry"
    assert manager.gate.is_paused()


def test_unsupported_desktop_keeps_gate_open():
    manager = _manager()

    manager.apply_snapshot(
        _snapshot("unknown", battery_present=False), now_ns=1_000
    )

    assert manager.status(now_ns=1_000).state == "unsupported"
    assert not manager.gate.is_paused()


def test_disabling_policy_hot_opens_gate():
    manager = _manager()
    manager.apply_snapshot(_snapshot("battery"), now_ns=1_000)

    manager.apply_settings(_settings(enabled=False))

    assert manager.status().state == "disabled"
    assert not manager.gate.is_paused()


@pytest.mark.asyncio
async def test_gate_waiter_resumes_without_polling():
    gate = InferencePauseGate()
    gate.set_paused(True, "test")
    waiter = asyncio.create_task(gate.wait_until_resumed())
    await asyncio.sleep(0)
    assert not waiter.done()

    gate.set_paused(False)

    await asyncio.wait_for(waiter, timeout=0.1)


def test_gate_records_first_cooperative_pause_boundary():
    gate = InferencePauseGate()
    gate.set_max_work_quantum_seconds(0.25)
    gate.set_paused(True, "test")

    gate.acknowledge_pause_boundary()

    assert gate.max_work_quantum_seconds == 0.25
    assert gate.last_pause_response_seconds is not None
    assert gate.last_pause_response_seconds >= 0.0


def test_sync_gate_wait_can_be_cancelled_while_paused():
    gate = InferencePauseGate()
    stop_event = threading.Event()
    gate.set_paused(True, "test")
    stop_event.set()

    assert gate.wait_until_resumed_sync(stop_event) is False
    assert gate.last_pause_response_seconds is not None


def test_below_floor_starts_fully_paused_charge_recovery():
    manager = _manager()

    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=5.0), now_ns=1_000
    )

    status = manager.status(now_ns=1_000)
    assert status.state == "charge_recovery"
    assert status.inference_duty == 0.0
    assert manager.gate.is_paused()


def test_stable_idle_headroom_issues_small_configured_probe():
    manager = _manager()
    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=15.0), now_ns=0
    )

    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=15.0),
        now_ns=3_100_000_000,
    )

    status = manager.status(now_ns=3_100_000_000)
    assert status.inference_duty == 0.05
    assert status.idle_charge_baseline_watts == pytest.approx(15.0)
    assert not manager.gate.is_paused()


def test_charge_shortfall_reduces_duty_faster_than_restoration():
    manager = _manager()
    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=15.0), now_ns=0
    )
    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=15.0),
        now_ns=3_100_000_000,
    )
    assert manager.status(now_ns=3_100_000_000).inference_duty == 0.05

    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=0.0),
        now_ns=3_200_000_000,
    )
    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=0.0),
        now_ns=3_800_000_000,
    )

    assert manager.status(now_ns=3_800_000_000).inference_duty == 0.0
    assert manager.gate.is_paused()


def test_recovery_exits_only_at_floor_plus_hysteresis():
    manager = _manager()
    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=15.0), now_ns=0
    )

    manager.apply_snapshot(
        _snapshot("ac", charge_percent=51.9, charge_watts=15.0), now_ns=1_000
    )
    assert manager.status(now_ns=1_000).state == "charge_recovery"

    manager.apply_snapshot(
        _snapshot("ac", charge_percent=52.0, charge_watts=15.0), now_ns=2_000
    )
    status = manager.status(now_ns=2_000)
    assert status.state == "normal"
    assert status.inference_duty == 1.0
    assert not manager.gate.is_paused()


def test_automatic_deadband_is_bounded_by_user_settings():
    manager = _manager(_settings(charge_deadband_watts=None))

    for index, watts in enumerate((8.0, 12.0, 8.0, 12.0)):
        manager.apply_snapshot(
            _snapshot("ac", charge_percent=80.0, charge_watts=watts),
            now_ns=index * 250_000_000,
        )

    deadband = manager.status(now_ns=1_000_000_000).effective_charge_deadband_watts
    assert deadband is not None
    assert 1.0 <= deadband <= 5.0


def test_stale_charge_watts_forces_recovery_duty_to_zero():
    manager = _manager()
    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=15.0), now_ns=0
    )
    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=15.0),
        now_ns=3_100_000_000,
    )
    assert manager.status(now_ns=3_100_000_000).inference_duty == 0.05

    manager.apply_snapshot(
        _snapshot("ac", charge_percent=49.0, charge_watts=None),
        now_ns=5_200_000_001,
    )

    assert manager.status(now_ns=5_200_000_001).inference_duty == 0.0
    assert manager.gate.is_paused()
