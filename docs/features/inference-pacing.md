# Inference pacing

- **Feature ID:** `inference-pacing`
- **Standalone source:** `ee978a1c466323aa366f82bb831e99fe423583ea`.
- **Standalone baseline:** `25aebb3ee0bb052a264241953bef6820f102dd0b`.
- **Also present inside:** combined source `bd850747e6336ee59e6fc9242a865eda5257d061`, based on `73322ed188a25b9ce2e490d3137d810f7d50128d`.
- **Status:** The standalone source is an alternative. Its branch validation does not qualify the different combined integration on another target.

The standalone version adds a live, best-effort work-share target for supported
local batched generation. It does not require battery protection, model turns,
or hierarchical allowances. Its engine coverage is narrower than the combined
controls. Use the standalone source **or** the combined bundle's pacer; never
layer both pacers into one engine.

In the combined source, `InferencePacer.acquire()`, `begin_step()` and
`complete()` in `omlx/inference_pacing.py` own shared work/rest accounting.
`ModelPacer` in `omlx/model_pacing.py` adapts that shared pacer to each model.
`EngineCore._acquire_execution_pacing()` and
`_finish_execution_pacing()` connect permits to safe batched steps; engine and
scheduler hooks report activity and cancellation. `EnginePool` owns pacer
instances and shares. These hooks, not the settings switch alone, implement
the behavior.

For exact snapshots and the difference between standalone and bundled pacing,
see the [inference adoption map](../inference-adoption-map.md#runtime-components-and-real-seams)
and the [throttle contract](https://github.com/actualrealhuman/omlx/blob/ee978a1c466323aa366f82bb831e99fe423583ea/docs/decisions/inference-throttle.md).

Qualify the selected source on its exact target: 100% full-speed behavior,
reduced work share, live updates, cancellation/cleanup, multiple engines sharing
one process pacer, supported text and VLM steps, and MTP work-time accounting.
Inspect `tests/test_inference_pacing.py`, `tests/test_model_pacing.py`,
`tests/test_admin_inference_throttle_ui.py`, `tests/test_mlx_lm_mtp_patch.py`,
`tests/test_prefill_oom_graceful.py` and relevant `tests/test_settings.py`
cases. They are validation references, not new test results for a different
target. Percentages describe submission activity, not watts or proportional
tokens/second; work already submitted may complete.

Return to the [catalog](../FORK_FEATURES.md) or [composition guide](../FEATURE_COMPOSITION.md).

[Open the fixed implementation source](https://github.com/actualrealhuman/omlx/tree/ee978a1c466323aa366f82bb831e99fe423583ea).
