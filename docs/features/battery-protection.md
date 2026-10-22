# Battery reserve protection

- **Feature ID:** `battery-protection`
- **Source snapshot:** `bd850747e6336ee59e6fc9242a865eda5257d061`.
- **Baseline:** `73322ed188a25b9ce2e490d3137d810f7d50128d`.
- **Status:** Coupled component of the combined bundle; not independently extracted or qualified.

Opt-in battery protection reduces the effective submission share below its
throttle threshold, suspends supported work at the reserve, handles external
power loss and restores share gradually. Defaults are off; the documented
default curve begins reducing below 75%, suspends at 50%, and resumes at 55%.
The master user-controls bypass does not disable battery suspension.

`BatteryProtectionSettings` and `BatteryPolicy.evaluate()` in
`omlx/battery_protection.py` contain validated policy and state transitions.
`BatteryProtectionService._read_sample()`, `_evaluate()`, `sample_now()` and
`status()` in `omlx/battery_service.py` connect observations to share and gate
updates. `PowerSourceReader` and `PowerSourceChangeNotifier` are in
`omlx/utils/power_sources.py`; `GenerationGate` is in
`omlx/generation_gate.py`. `EnginePool` composes the requested and effective
share, while `EngineCore` and scheduler checks honor the gate at cooperative
boundaries. Settings, admin APIs/status and the dashboard must accompany the
policy.

The effective share is a scheduler submission limit, not a hardware power cap.
Already-submitted device work can finish; audio, embeddings, reranking,
diffusion, remote/distributed execution, loading, separate processes and
independent device work are outside this contract. `BatteryProtectionService`
currently does not have a telemetry-only mode. For a telemetry-only source,
follow the explicit extraction checklist in the
[adoption map](../inference-adoption-map.md#concrete-extraction-checklist-telemetry-without-a-governor).

The coherent adoption unit is the
[combined inference-controls bundle](inference-controls.md). Inspect
`tests/test_battery_protection.py`, `tests/test_battery_service.py`,
`tests/test_battery_generation.py`, `tests/test_battery_model_shares.py`,
`tests/test_inference_master_runtime.py`, and
`apps/omlx-web/tests/test_admin_battery_protection_ui.py`. On the target, use
synthetic samples for opt-out/default behavior, thresholds/hysteresis, stale or
missing telemetry, outage caps, gradual recovery, user-ceiling composition,
master bypass, cancellation while suspended, and supported VLM preparation.
These checks are not a result for an isolated protection-only patch.

Return to the [catalog](../FORK_FEATURES.md) or [composition guide](../FEATURE_COMPOSITION.md).

[Open the fixed implementation source](https://github.com/actualrealhuman/omlx/tree/bd850747e6336ee59e6fc9242a865eda5257d061).
