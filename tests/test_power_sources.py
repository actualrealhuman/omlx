# SPDX-License-Identifier: Apache-2.0
"""Tests for documented macOS power-source telemetry."""

from omlx.utils.power_sources import _charge_watts, _source_name


def test_source_name_normalizes_iokit_values():
    assert _source_name("AC Power") == "ac"
    assert _source_name("Battery Power") == "battery"
    assert _source_name("UPS Power") == "ups"
    assert _source_name(None) == "unknown"


def test_negative_apple_current_is_positive_charging_power():
    watts = _charge_watts(-1_000, 12_000, charging=True, source="ac")
    assert watts == 12.0


def test_positive_apple_current_is_negative_battery_power():
    watts = _charge_watts(8_000, 12_000, charging=False, source="battery")
    assert watts == -96.0


def test_state_constraints_correct_an_inverted_current_sign():
    assert _charge_watts(1_000, 12_000, charging=True, source="ac") == 12.0
    assert _charge_watts(-1_000, 12_000, charging=False, source="battery") == -12.0


def test_missing_or_invalid_electrical_reading_is_unavailable():
    assert _charge_watts(None, 12_000, charging=True, source="ac") is None
    assert _charge_watts(-1_000, None, charging=True, source="ac") is None
    assert _charge_watts(-1_000, 0, charging=True, source="ac") is None
