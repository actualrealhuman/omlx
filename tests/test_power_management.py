# SPDX-License-Identifier: Apache-2.0
"""Tests for battery policy and the cooperative inference gate."""

import asyncio
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
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot(source, *, battery_present=True):
    return PowerSourceSnapshot(
        monotonic_ns=0,
        source=source,
        battery_present=battery_present,
        adapter_present=source == "ac",
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
