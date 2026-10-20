# SPDX-License-Identifier: Apache-2.0
"""Policy tests require only the standard library; no models or MLX."""

import math
import unittest
from dataclasses import replace

from omlx.battery_protection import (
    BatteryPolicy,
    BatteryProtectionSettings,
    BatteryRecoveryState,
    BatterySample,
)


def sample(now=0.0, battery=80.0, ac=True):
    return BatterySample(
        now, battery_present=True, charge_percent=battery, external_power=ac
    )


class BatteryPolicyTests(unittest.TestCase):
    def policy(self, recovery=None):
        return BatteryPolicy(BatteryProtectionSettings(enabled=True), recovery)

    def test_curve_and_user_maximum(self):
        for maximum, expected in ((1.0, 0.64), (0.75, 0.49), (0.1, 0.1), (0.2, 0.16)):
            with self.subTest(maximum=maximum):
                result = self.policy().evaluate(sample(battery=65), maximum, now=0)
                self.assertAlmostEqual(result.effective_share, expected)
        for battery, expected in (
            (75, 1),
            (70, 0.82),
            (60, 0.46),
            (55, 0.28),
            (51, 0.136),
        ):
            self.assertAlmostEqual(
                self.policy().evaluate(sample(battery=battery), 1, now=0).target_share,
                expected,
            )

    def test_configurable_curve_floor_and_lower_requested_maximum(self):
        configured = BatteryProtectionSettings.from_options(
            True, {"minimum_share": 0.2, "throttle_start_percent": 80}
        )
        policy = BatteryPolicy(configured)
        result = policy.evaluate(sample(battery=65), 1, now=0)
        self.assertAlmostEqual(result.target_share, 0.6)
        lower = policy.evaluate(sample(1, battery=65), 0.15, now=1)
        self.assertAlmostEqual(lower.target_share, 0.15)
        self.assertLessEqual(lower.effective_share, 0.15)

    def test_custom_minimum_is_used_after_reserve_suspend(self):
        policy = BatteryPolicy(
            BatteryProtectionSettings.from_options(True, {"minimum_share": 0.3})
        )
        self.assertTrue(policy.evaluate(sample(battery=50), 1, now=0).suspended)
        resumed = policy.evaluate(sample(1, battery=55), 1, now=1)
        self.assertAlmostEqual(resumed.target_share, 0.44)
        self.assertAlmostEqual(resumed.effective_share, 0.3)

    def test_exact_reserve_boundary_and_hysteresis(self):
        policy = self.policy()
        self.assertTrue(policy.evaluate(sample(battery=50), 1, now=0).suspended)
        self.assertTrue(policy.evaluate(sample(1, 54.9), 1, now=1).suspended)
        restored = policy.evaluate(sample(2, 55), 1, now=2)
        self.assertFalse(restored.suspended)
        self.assertLess(restored.effective_share, restored.target_share)

    def test_short_outage_and_stable_restoration(self):
        policy = self.policy()
        policy.evaluate(sample(), 1, now=0)
        self.assertEqual(
            policy.evaluate(sample(1, ac=False), 1, now=1).effective_share, 0.25
        )
        self.assertEqual(policy.evaluate(sample(11), 1, now=11).state, "ac_stabilizing")
        restored = policy.evaluate(sample(21), 1, now=21)
        self.assertEqual(restored.target_share, 1)
        self.assertTrue(restored.restoring)

    def test_extended_outage_and_flicker(self):
        policy = self.policy()
        policy.evaluate(sample(), 1, now=0)
        policy.evaluate(sample(1, ac=False), 1, now=1)
        policy.evaluate(sample(40), 1, now=40)
        policy.evaluate(sample(42, ac=False), 1, now=42)
        self.assertEqual(
            policy.evaluate(sample(61, ac=False), 1, now=61).effective_share, 0.1
        )

    def test_start_on_battery_has_no_new_grace(self):
        result = self.policy().evaluate(sample(ac=False), 1, now=0)
        self.assertEqual(result.state, "battery_emergency")
        self.assertEqual(result.effective_share, 0.1)

    def test_reserve_overrides_grace_and_ac_restoration(self):
        policy = self.policy()
        policy.evaluate(sample(), 1, now=0)
        policy.evaluate(sample(1, ac=False), 1, now=1)
        self.assertTrue(policy.evaluate(sample(2, 50, False), 1, now=2).suspended)
        self.assertTrue(policy.evaluate(sample(3, 52), 1, now=3).suspended)
        self.assertTrue(policy.evaluate(sample(20, 54), 1, now=20).suspended)

    def test_recovery_survives_restart(self):
        policy = self.policy()
        policy.evaluate(sample(battery=49), 1, now=0)
        recovery = BatteryRecoveryState.from_dict(policy.recovery.__dict__)
        restarted = self.policy(recovery)
        self.assertTrue(restarted.evaluate(sample(0, 52), 1, now=0).suspended)
        self.assertFalse(restarted.evaluate(sample(1, 55), 1, now=1).suspended)

    def test_restart_during_outage_requalifies_restoration(self):
        policy = self.policy(
            BatteryRecoveryState(outage_active=True, battery_seen=True)
        )
        self.assertEqual(policy.evaluate(sample(), 1, now=0).target_share, 0.1)
        self.assertEqual(policy.evaluate(sample(10), 1, now=10).target_share, 1)

    def test_stale_telemetry_cannot_restore(self):
        policy = self.policy()
        policy.evaluate(sample(), 1, now=0)
        result = policy.evaluate(sample(), 1, now=6)
        self.assertTrue(result.suspended)
        self.assertEqual(result.state, "telemetry_unavailable")
        self.assertTrue(policy.evaluate(sample(7, 50), 1, now=7).suspended)

    def test_missing_percent_or_connection_is_not_zero_or_outage(self):
        for invalid in (None, math.nan, math.inf, -1, 101, True):
            result = self.policy().evaluate(
                replace(sample(), charge_percent=invalid), 1, now=0
            )
            self.assertTrue(result.suspended)
            self.assertIsNone(result.battery_percent)
        self.assertTrue(
            self.policy()
            .evaluate(replace(sample(), external_power=None), 1, now=0)
            .suspended
        )

    def test_desktop_unsupported_and_disabled(self):
        self.assertFalse(
            self.policy()
            .evaluate(BatterySample(0, battery_present=False), 1, now=0)
            .suspended
        )
        self.assertEqual(
            self.policy().evaluate(BatterySample(0, supported=False), 1, now=0).state,
            "unsupported",
        )
        disabled = BatteryPolicy().evaluate(sample(battery=1, ac=False), 0.75, now=0)
        self.assertEqual(disabled.effective_share, 0.75)

    def test_disable_does_not_erase_reserve_latch(self):
        policy = self.policy()
        policy.evaluate(sample(battery=49), 1, now=0)
        policy.settings = replace(policy.settings, enabled=False)
        self.assertFalse(policy.evaluate(sample(1, 52), 1, now=1).suspended)
        policy.settings = replace(policy.settings, enabled=True)
        self.assertTrue(policy.evaluate(sample(2, 52), 1, now=2).suspended)

    def test_user_changes_recalculate_curve(self):
        policy = self.policy()
        policy.evaluate(sample(battery=65), 1, now=0)
        self.assertAlmostEqual(
            policy.evaluate(sample(1, 65), 0.2, now=1).effective_share, 0.16
        )
        raised = policy.evaluate(sample(2, 65), 1, now=2)
        self.assertAlmostEqual(raised.target_share, 0.64)
        self.assertAlmostEqual(raised.effective_share, 0.17)

    def test_low_user_maximum_wins_over_emergency_cap(self):
        result = self.policy().evaluate(sample(ac=False), 0.1, now=0)
        self.assertEqual(result.effective_share, 0.1)

    def test_validation(self):
        for kwargs in (
            {"enabled": 1},
            {"suspend_percent": 75},
            {"outage_share": 0.05},
            {"telemetry_stale_seconds": math.nan},
            {"telemetry_stale_seconds": 0.5},
            {"restoration_share_per_second": 1.01},
        ):
            with self.assertRaises(ValueError):
                BatteryProtectionSettings(**kwargs)
        BatteryProtectionSettings(outage_grace_seconds=0, ac_stabilization_seconds=0)
        assert self.policy().evaluate(sample(), 0, now=0).effective_share == 0
        for maximum in (-0.01, True, math.nan, 1.1):
            with self.assertRaises(ValueError):
                self.policy().evaluate(sample(), maximum, now=0)
        with self.assertRaises(ValueError):
            BatteryRecoveryState.from_dict({"reserve_suspended": "false"})

    def test_settings_config_round_trip_and_strict_option_map(self):
        original = BatteryProtectionSettings.from_options(
            True, {"minimum_share": 0.2, "throttle_start_percent": 80}
        )
        self.assertEqual(
            BatteryProtectionSettings.from_dict(original.to_dict()), original
        )
        with self.assertRaises(ValueError):
            BatteryProtectionSettings.from_options(True, {"enabled": False})


if __name__ == "__main__":
    unittest.main()
