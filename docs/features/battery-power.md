# Historical battery power governor and telemetry

- **Feature ID:** `battery-power`
- **Historical source:** `3073fc3f773b0ccfdd4bf39c3c5f1adaf94bb186`.
- **Historical baseline:** `ebf4f1faaf7791fb073a220ddf713cfd90caa0b7`.
- **Status:** Historical active governor source; no telemetry-only adoption unit is published here.

The original battery-power implementation combines power observations with an
active process-wide governor. `BatteryPowerManager` in
`omlx/power_management.py` operates `InferencePauseGate`, pauses inference when
macOS reports battery as the effective source, stabilizes AC restoration, and
uses a charge-recovery duty controller below its configured charge floor.
`PowerSourceReader.sample()` and `PowerSourceChangeNotifier` in
`omlx/utils/power_sources.py` provide the OS observation layer. `PowerManagementSettings`
are persisted under `GlobalSettings.power`; `omlx/server.py`, engine core and
scheduler supply the service lifecycle and cooperative work boundary.

This governor is not the later opt-in battery reserve policy in
`battery_protection.py`. The later policy caps a requested work share using
charge thresholds, external-power outage rules and recovery ramps. Do not
combine both governors or generation gates without reconciling who owns pause,
duty and restoration decisions.

`PowerSourceReader` and its notifier can inform a future read-only telemetry
feature, but copying either historical governor or current
`BatteryProtectionService` does not make the result telemetry-only. At the
combined source `bd850747`, `BatteryProtectionService` still evaluates policy
and can change shares and close `GenerationGate`; it has no `telemetry_only`
mode. Follow the [telemetry-only extraction checklist](../inference-adoption-map.md#concrete-extraction-checklist-telemetry-without-a-governor)
before claiming that narrower unit.

For the historical governor, inspect `docs/power-management.md`,
`omlx/power_management.py`, `omlx/utils/power_sources.py`,
`omlx/settings.py`, `omlx/server.py`, `omlx/engine_core.py` and
`omlx/scheduler.py` at the source commit. Relevant historical checks are
`tests/test_power_sources.py`, `tests/test_power_management.py`,
`tests/test_engine_core.py`, `tests/test_decode_fairness.py` and
`tests/test_settings.py`. These source/test references do not establish
compatibility with the combined controls or a new target. Use synthetic
telemetry for source changes; do not drain a physical battery as a test.

Return to the [catalog](../FORK_FEATURES.md) or [composition guide](../FEATURE_COMPOSITION.md).

[Open the fixed implementation source](https://github.com/actualrealhuman/omlx/tree/3073fc3f773b0ccfdd4bf39c3c5f1adaf94bb186).
