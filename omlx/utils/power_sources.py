# SPDX-License-Identifier: Apache-2.0
"""Unprivileged macOS battery and external-power telemetry.

This module deliberately limits itself to the documented IOPowerSources and
CoreFoundation interfaces. It does not use ``powermetrics``, direct SMC access,
or manually declared IOReport symbols.

Positive ``battery_charge_watts`` means energy is entering the battery;
negative values mean the battery is supplying the system.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from dataclasses import asdict, dataclass
from typing import Literal

PowerSource = Literal["ac", "battery", "ups", "unknown"]

_CF_STRING_ENCODING_UTF8 = 0x08000100
_CF_NUMBER_SINT64_TYPE = 4


@dataclass(frozen=True)
class PowerSourceSnapshot:
    """One host power-source reading.

    Electrical units match the documented IOPowerSources keys. ``None``
    distinguishes an unavailable sensor from a real zero.
    """

    monotonic_ns: int
    source: PowerSource
    battery_present: bool
    adapter_present: bool
    charging: bool | None = None
    charge_percent: float | None = None
    current_milliamps: int | None = None
    voltage_millivolts: int | None = None
    battery_charge_watts: float | None = None
    adapter_watts: int | None = None


def _source_name(value: str | None) -> PowerSource:
    normalized = (value or "").strip().casefold()
    if normalized == "ac power":
        return "ac"
    if normalized == "battery power":
        return "battery"
    if normalized == "ups power":
        return "ups"
    return "unknown"


def _charge_watts(
    current_milliamps: int | None,
    voltage_millivolts: int | None,
    *,
    charging: bool | None,
    source: PowerSource,
) -> float | None:
    """Normalize Apple's signed battery current to charge-positive watts.

    Apple portable batteries publish negative current while charging and
    positive current while discharging. Charging state and effective source
    constrain the sign so a platform variation cannot invert the safety
    decision.
    """

    if current_milliamps is None or voltage_millivolts is None:
        return None
    if voltage_millivolts <= 0:
        return None

    watts = -(current_milliamps * voltage_millivolts) / 1_000_000.0
    if (charging is True and watts < 0) or (
        charging is not True and source == "battery" and watts > 0
    ):
        watts = -watts
    return watts


class _CoreFoundation:
    def __init__(self) -> None:
        self.lib = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self.lib.CFRelease.argtypes = [ctypes.c_void_p]
        self.lib.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self.lib.CFArrayGetCount.restype = ctypes.c_long
        self.lib.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self.lib.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        self.lib.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.lib.CFDictionaryGetValue.restype = ctypes.c_void_p
        self.lib.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.lib.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.lib.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self.lib.CFStringGetLength.restype = ctypes.c_long
        self.lib.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self.lib.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self.lib.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self.lib.CFStringGetCString.restype = ctypes.c_bool
        self.lib.CFNumberGetValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self.lib.CFNumberGetValue.restype = ctypes.c_bool
        self.lib.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        self.lib.CFBooleanGetValue.restype = ctypes.c_bool
        self._keys: dict[str, int] = {}

    def close(self) -> None:
        for ref in self._keys.values():
            self.lib.CFRelease(ref)
        self._keys.clear()

    def key(self, value: str) -> int:
        ref = self._keys.get(value)
        if ref is None:
            ref = int(
                self.lib.CFStringCreateWithCString(
                    None, value.encode("utf-8"), _CF_STRING_ENCODING_UTF8
                )
                or 0
            )
            if not ref:
                raise RuntimeError(f"Could not create CoreFoundation key {value!r}")
            self._keys[value] = ref
        return ref

    def dictionary_value(self, dictionary: int, key: str) -> int | None:
        if not dictionary:
            return None
        value = self.lib.CFDictionaryGetValue(dictionary, self.key(key))
        return int(value) if value else None

    def string(self, ref: int | None) -> str | None:
        if not ref:
            return None
        length = self.lib.CFStringGetLength(ref)
        size = self.lib.CFStringGetMaximumSizeForEncoding(
            length, _CF_STRING_ENCODING_UTF8
        ) + 1
        if size <= 0:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not self.lib.CFStringGetCString(
            ref, buffer, size, _CF_STRING_ENCODING_UTF8
        ):
            return None
        return buffer.value.decode("utf-8", errors="replace")

    def number(self, ref: int | None) -> int | None:
        if not ref:
            return None
        value = ctypes.c_int64()
        if not self.lib.CFNumberGetValue(
            ref, _CF_NUMBER_SINT64_TYPE, ctypes.byref(value)
        ):
            return None
        return int(value.value)

    def boolean(self, ref: int | None) -> bool | None:
        if not ref:
            return None
        return bool(self.lib.CFBooleanGetValue(ref))


class PowerSourceReader:
    """Read current host power state through IOPowerSources.

    Construction and sampling degrade cleanly on non-Darwin systems so the
    server can import this module everywhere it already supports.
    """

    def __init__(self) -> None:
        self._cf: _CoreFoundation | None = None
        self._iokit: ctypes.CDLL | None = None
        self._battery_service: int = 0
        if sys.platform != "darwin":
            return

        try:
            cf = _CoreFoundation()
            iokit = ctypes.CDLL(
                "/System/Library/Frameworks/IOKit.framework/IOKit"
            )
            iokit.IOPSCopyPowerSourcesInfo.restype = ctypes.c_void_p
            iokit.IOPSCopyPowerSourcesList.argtypes = [ctypes.c_void_p]
            iokit.IOPSCopyPowerSourcesList.restype = ctypes.c_void_p
            iokit.IOPSGetPowerSourceDescription.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            iokit.IOPSGetPowerSourceDescription.restype = ctypes.c_void_p
            iokit.IOPSGetProvidingPowerSourceType.argtypes = [ctypes.c_void_p]
            iokit.IOPSGetProvidingPowerSourceType.restype = ctypes.c_void_p
            iokit.IOPSCopyExternalPowerAdapterDetails.restype = ctypes.c_void_p
            iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
            iokit.IOServiceMatching.restype = ctypes.c_void_p
            iokit.IOServiceGetMatchingService.argtypes = [
                ctypes.c_uint,
                ctypes.c_void_p,
            ]
            iokit.IOServiceGetMatchingService.restype = ctypes.c_uint
            iokit.IORegistryEntryCreateCFProperty.argtypes = [
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint,
            ]
            iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
            iokit.IOObjectRelease.argtypes = [ctypes.c_uint]
            iokit.IOObjectRelease.restype = ctypes.c_int
            self._cf = cf
            self._iokit = iokit
            matching = iokit.IOServiceMatching(b"AppleSmartBattery")
            if matching:
                self._battery_service = int(
                    iokit.IOServiceGetMatchingService(0, matching)
                )
        except Exception:
            if self._cf is not None:
                self._cf.close()
            self._cf = None
            self._iokit = None

    @property
    def available(self) -> bool:
        return self._cf is not None and self._iokit is not None

    def close(self) -> None:
        cf, self._cf = self._cf, None
        iokit, self._iokit = self._iokit, None
        service, self._battery_service = self._battery_service, 0
        if iokit is not None and service:
            iokit.IOObjectRelease(service)
        if cf is not None:
            cf.close()

    def _registry_property(self, key: str) -> int:
        cf = self._cf
        iokit = self._iokit
        if cf is None or iokit is None or not self._battery_service:
            return 0
        return int(
            iokit.IORegistryEntryCreateCFProperty(
                self._battery_service, cf.key(key), None, 0
            )
            or 0
        )

    def _registry_number(self, key: str) -> int | None:
        cf = self._cf
        if cf is None:
            return None
        value = self._registry_property(key)
        try:
            return cf.number(value)
        finally:
            if value:
                cf.lib.CFRelease(value)

    def _registry_boolean(self, key: str) -> bool | None:
        cf = self._cf
        if cf is None:
            return None
        value = self._registry_property(key)
        try:
            return cf.boolean(value)
        finally:
            if value:
                cf.lib.CFRelease(value)

    def _registry_adapter_watts(self) -> int | None:
        cf = self._cf
        if cf is None:
            return None
        details = self._registry_property("AdapterDetails")
        try:
            return cf.number(cf.dictionary_value(details, "Watts"))
        finally:
            if details:
                cf.lib.CFRelease(details)

    def sample(self) -> PowerSourceSnapshot:
        now = time.monotonic_ns()
        cf = self._cf
        iokit = self._iokit
        if cf is None or iokit is None:
            return PowerSourceSnapshot(
                monotonic_ns=now,
                source="unknown",
                battery_present=False,
                adapter_present=False,
            )

        info = int(iokit.IOPSCopyPowerSourcesInfo() or 0)
        if not info:
            return PowerSourceSnapshot(
                monotonic_ns=now,
                source="unknown",
                battery_present=False,
                adapter_present=False,
            )

        source: PowerSource = "unknown"
        battery_present = False
        charging: bool | None = None
        charge_percent: float | None = None
        current_ma: int | None = None
        voltage_mv: int | None = None
        sources = 0
        try:
            source = _source_name(
                cf.string(int(iokit.IOPSGetProvidingPowerSourceType(info) or 0))
            )
            sources = int(iokit.IOPSCopyPowerSourcesList(info) or 0)
            if sources:
                count = cf.lib.CFArrayGetCount(sources)
                for index in range(count):
                    handle = cf.lib.CFArrayGetValueAtIndex(sources, index)
                    description = int(
                        iokit.IOPSGetPowerSourceDescription(info, handle) or 0
                    )
                    source_type = cf.string(
                        cf.dictionary_value(description, "Type")
                    )
                    if source_type != "InternalBattery":
                        continue

                    present = cf.boolean(
                        cf.dictionary_value(description, "Is Present")
                    )
                    battery_present = present is not False
                    if not battery_present:
                        break

                    charging = cf.boolean(
                        cf.dictionary_value(description, "Is Charging")
                    )
                    current = cf.number(
                        cf.dictionary_value(description, "Current Capacity")
                    )
                    maximum = cf.number(
                        cf.dictionary_value(description, "Max Capacity")
                    )
                    if current is not None and maximum is not None and maximum > 0:
                        charge_percent = max(0.0, min(100.0, current * 100 / maximum))
                    current_ma = cf.number(
                        cf.dictionary_value(description, "Current")
                    )
                    voltage_mv = cf.number(
                        cf.dictionary_value(description, "Voltage")
                    )
                    break
        finally:
            if sources:
                cf.lib.CFRelease(sources)
            cf.lib.CFRelease(info)

        # Apple-defined power sources normally publish Current and Voltage in
        # the description dictionary. Some Tahoe builds omit one or both from
        # that normalized view while retaining the documented IOPMPowerSource
        # reportable properties on AppleSmartBattery, so read those properties
        # directly as a no-subprocess fallback.
        if current_ma is None:
            current_ma = self._registry_number("Amperage")
        if voltage_mv is None:
            voltage_mv = self._registry_number("Voltage")

        adapter = int(iokit.IOPSCopyExternalPowerAdapterDetails() or 0)
        adapter_watts: int | None = None
        try:
            if adapter:
                adapter_watts = cf.number(cf.dictionary_value(adapter, "Watts"))
        finally:
            if adapter:
                cf.lib.CFRelease(adapter)
        if adapter_watts is None:
            adapter_watts = self._registry_adapter_watts()
        external_connected = self._registry_boolean("ExternalConnected")

        return PowerSourceSnapshot(
            monotonic_ns=now,
            source=source,
            battery_present=battery_present,
            adapter_present=bool(adapter) or external_connected is True,
            charging=charging,
            charge_percent=charge_percent,
            current_milliamps=current_ma,
            voltage_millivolts=voltage_mv,
            battery_charge_watts=_charge_watts(
                current_ma,
                voltage_mv,
                charging=charging,
                source=source,
            ),
            adapter_watts=adapter_watts,
        )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()

    reader = PowerSourceReader()
    try:
        for index in range(max(1, args.count)):
            print(json.dumps(asdict(reader.sample()), sort_keys=True), flush=True)
            if index + 1 < args.count:
                time.sleep(max(0.0, args.interval))
    finally:
        reader.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual hardware probe
    raise SystemExit(_main())
