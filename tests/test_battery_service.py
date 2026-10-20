# SPDX-License-Identifier: Apache-2.0
"""Exercise service/gate behavior with fake sensors; never sample host power."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omlx.battery_service import BatteryProtectionService
from omlx.generation_gate import GenerationGate, GenerationSuspendedError


class Reader:
    def __init__(self):
        self.raw = SimpleNamespace(
            source="ac",
            adapter_present=True,
            battery_present=True,
            charge_percent=80,
            charging=True,
        )
        self.closed = False

    def sample(self):
        return self.raw

    def close(self):
        self.closed = True


class Notifier:
    def changed(self):
        return False

    def close(self):
        pass


class BatteryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.platform = patch("omlx.battery_service.sys.platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.path = Path(self.temporary.name) / "state.json"
        self.reader = Reader()
        self.gate = GenerationGate()
        self.now = 0.0
        self.applied = []

    def service(self, enabled=True):
        return BatteryProtectionService(
            enabled=enabled,
            requested_share=1,
            apply_share=lambda share: self.applied.append(
                (share, self.gate.paused_snapshot)
            ),
            state_path=self.path,
            reader=self.reader,
            notifier=Notifier(),
            gate=self.gate,
            clock=lambda: self.now,
        )

    def test_live_maximum_change_cannot_override_battery_limit(self):
        self.reader.raw.charge_percent = 65
        service = self.service()
        service.sample_now()
        self.assertAlmostEqual(self.applied[-1][0], 0.64)
        service.set_requested_share(0.2)
        self.assertAlmostEqual(self.applied[-1][0], 0.16)
        self.assertEqual(service.status()["configured_share"], 0.2)

    def test_invalid_live_setting_does_not_poison_monitor(self):
        service = self.service()
        service.sample_now()
        with self.assertRaises(ValueError):
            service.set_requested_share(float("nan"))
        service.sample_now()
        self.assertEqual(service.status()["configured_share"], 1)

    def test_live_policy_configuration_is_visible_and_keeps_reserve_latch(self):
        from omlx.battery_protection import BatteryProtectionSettings

        self.reader.raw.charge_percent = 50
        service = self.service()
        service.sample_now()
        service.configure(
            BatteryProtectionSettings.from_options(
                True, {"minimum_share": 0.3, "resume_percent": 56}
            )
        )
        self.reader.raw.charge_percent = 55
        self.now = 1
        service.sample_now()
        self.assertTrue(service.status()["suspended"])
        self.assertEqual(service.status()["policy"]["minimum_share"], 0.3)
        self.reader.raw.charge_percent = 56
        self.now = 2
        service.sample_now()
        self.assertFalse(service.status()["suspended"])
        self.assertEqual(service.status()["policy"]["resume_percent"], 56)

    def test_latch_persists_and_restart_cannot_bypass_it(self):
        self.reader.raw.charge_percent = 50
        service = self.service()
        service.sample_now()
        self.assertTrue(self.gate.paused_snapshot)
        self.assertTrue(json.loads(self.path.read_text())["reserve_suspended"])
        self.reader.raw.charge_percent = 52
        self.assertTrue(self.service().policy.recovery.reserve_suspended)
        restarted = self.service()
        restarted.sample_now()
        self.assertTrue(restarted.status()["suspended"])

    def test_pacing_is_applied_before_suspended_gate_reopens(self):
        self.reader.raw.charge_percent = 49
        service = self.service()
        service.sample_now()
        self.reader.raw.charge_percent = 55
        self.now = 100
        service.sample_now()
        self.assertEqual(self.applied[-1], (0.1, True))
        self.assertFalse(self.gate.paused_snapshot)

    def test_failed_observation_does_not_refresh_stale_sample(self):
        service = self.service()
        service.sample_now()
        self.now = 6
        self.reader.sample = lambda: (_ for _ in ()).throw(OSError("unavailable"))
        with self.assertLogs("omlx.battery_service", level="WARNING"):
            service.sample_now()
        self.assertTrue(self.gate.paused_snapshot)
        self.assertEqual(service.status()["state"], "telemetry_unavailable")

    def test_unknown_source_is_not_a_desktop_or_an_outage(self):
        self.reader.raw.source = "unknown"
        service = self.service()
        service.sample_now()
        self.assertTrue(service.status()["suspended"])
        self.assertIsNone(service.status()["external_power"])

    def test_sensor_initialization_failure_does_not_crash_server(self):
        self.reader = None
        with patch(
            "omlx.battery_service.PowerSourceReader", side_effect=OSError("unavailable")
        ):
            with self.assertLogs("omlx.battery_service", level="WARNING"):
                service = self.service()
        service.sample_now()
        self.assertTrue(service.status()["suspended"])
        service.set_enabled(False)
        self.assertFalse(service.status()["suspended"])

    def test_adapter_present_while_discharging_uses_reserve_curve(self):
        self.reader.raw.source = "battery"
        self.reader.raw.charging = False
        self.reader.raw.charge_percent = 65
        service = self.service()
        service.sample_now()
        self.assertEqual(service.status()["state"], "battery_throttled")
        self.assertAlmostEqual(service.status()["effective_share"], 0.64)

    def test_disabled_monitor_does_not_create_recovery_file(self):
        service = self.service(enabled=False)
        service.sample_now()
        self.assertFalse(self.path.exists())
        self.assertEqual(service.status()["effective_share"], 1)

    def test_corrupt_recovery_requires_observed_recovery(self):
        self.path.write_text('{"reserve_suspended": "false"}')
        self.reader.raw.charge_percent = 52
        with self.assertLogs("omlx.battery_service", level="WARNING"):
            service = self.service()
        service.sample_now()
        self.assertTrue(service.status()["suspended"])


class GenerationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_rejects_admission_and_resume_wakes(self):
        gate = GenerationGate()
        gate.set_state(paused=True, restricted=True, reason="reserve_suspended")
        with self.assertRaises(GenerationSuspendedError):
            gate.check_admission()
        waiter = asyncio.create_task(gate.wait())
        await asyncio.sleep(0)
        gate.set_state(paused=False, restricted=False)
        await asyncio.wait_for(waiter, 1)
        self.assertEqual(gate._waiters, {})

    async def test_maintenance_wakes_without_opening_gate(self):
        gate = GenerationGate()
        gate.set_state(paused=True, restricted=True)
        wake = asyncio.Event()
        waiter = asyncio.create_task(gate.wait(wake))
        await asyncio.sleep(0)
        wake.set()
        await asyncio.wait_for(waiter, 1)
        self.assertTrue(gate.paused_snapshot)

    async def test_cancelled_wait_unregisters(self):
        gate = GenerationGate()
        gate.set_state(paused=True, restricted=True)
        waiter = asyncio.create_task(gate.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(gate._waiters, {})

    async def test_sync_checkpoint_can_cancel_without_resume(self):
        gate = GenerationGate()
        gate.set_state(paused=True, restricted=True)
        cancelled = threading.Event()
        waiter = asyncio.create_task(
            asyncio.to_thread(gate.wait_sync, cancelled.is_set)
        )
        cancelled.set()
        self.assertFalse(await asyncio.wait_for(waiter, 1))
        self.assertTrue(gate.paused_snapshot)
