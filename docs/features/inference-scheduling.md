# Model execution turns

- **Feature ID:** `inference-scheduling`
- **Source snapshot:** `bd850747e6336ee59e6fc9242a865eda5257d061`.
- **Baseline:** `73322ed188a25b9ce2e490d3137d810f7d50128d`.
- **Status:** Coupled component of the combined bundle; extraction and independent qualification are pending.

Serial model scheduling lets several supported models stay resident while one
model session owns inference work at a time. Requests for the selected model
can still batch. The default contested turn is five seconds. Priority, class,
equal-priority rotation, cancellation and safe handoff determine who runs.

`ModelExecutionCoordinator` in `omlx/inference_scheduling.py` owns registered
models and runnable work. `ModelRegistration.work()`, `ModelWork.checkpoint()`
and `ModelSubmission.release()` define work lifetime and release. `EngineCore`
registers the model, enters `_execution_context()`, drains submitted device
work in `_drain_model_execution()`, and supplies scheduler checkpoints.
`EnginePool.configure_inference_scheduling()` and
`configure_model_scheduling()` connect loaded/future model lifecycle, priority,
admission and settings. `scheduler.py` participates at prefill/decode/maintenance
boundaries. Safe work ownership, teardown and cleanup are part of the feature;
the coordinator module alone is not a reusable scheduler plugin.

This source shares `ModelPacer`, `InferencePacer`, settings/API and model
allowance policy. The runtime `inference_scheduling_enabled: false` switch
turns off turns, but does not remove those source couplings. The master
`inference_controls_enabled: false` bypasses user scheduling enforcement while
preserving battery protection. The whole
[combined bundle](inference-controls.md) is the documented adoption unit.

For detailed seams, see the [adoption map](../inference-adoption-map.md).
Inspect `tests/test_inference_scheduling.py`,
`tests/test_inference_scheduling_status.py`,
`tests/test_model_execution_integration.py`, `tests/test_model_pacing.py`,
`tests/test_admin_model_scheduling_api.py` and
`apps/omlx-web/tests/inference_scheduling_ui.test.cjs`. Qualification on a
target must cover disabled behavior, two resident models with overlapping
requests, priority, equal-priority rotation, cancellation, handoff/drain,
preparation and maintenance, and status. Cooperative turns do not preempt a
kernel. Strict priority can starve lower-priority work. These are checks to
perform, not evidence of an isolated component build.

Return to the [catalog](../FORK_FEATURES.md) or [composition guide](../FEATURE_COMPOSITION.md).

[Open the fixed implementation source](https://github.com/actualrealhuman/omlx/tree/bd850747e6336ee59e6fc9242a865eda5257d061).
