# Hierarchical inference allowances

- **Feature ID:** `inference-allowances`
- **Source snapshot:** `bd850747e6336ee59e6fc9242a865eda5257d061`.
- **Baseline:** `73322ed188a25b9ce2e490d3137d810f7d50128d`.
- **Status:** Coupled bundle component; no chart-only or allowance-only source is independently qualified.

The effective configured model ceiling is global share × class allowance ×
model allowance. With serial scheduling enabled, class and numeric priority
also influence turns. The persisted master bypass retains preferences while
bypassing user ceilings and serial turns; battery restrictions continue to
apply.

In `omlx/engine_pool.py`, `configure_inference_share()`,
`configure_inference_allowances()`, `configure_model_scheduling()` and
`_apply_model_policy()` compute and apply these values. `ServerSettings` in
`omlx/settings.py` owns the global ceiling, class allowances, scheduling,
master and battery settings. `ModelSettings` in `omlx/model_settings.py` owns
per-model allowance, priority and class/exclusive preference. Its
`ModelSettingsManager.migrate_inference_allowances()` converts legacy absolute
shares to relative values once with backup/failure handling. Authenticated
settings and scheduling APIs/status in `omlx/admin/routes.py`, plus the
dashboard controller/templates/translations, persist, confirm and display the
policy. `apps/omlx-web/tests/inference_scheduling_ui.test.cjs` covers chart and
selection behavior; the chart is not enforcement by itself.

Adopt this only as part of the
[combined bundle](inference-controls.md) until migration, global/model API,
runtime composition, user interface and persisted settings have been extracted
and qualified together. See the [source map](../inference-adoption-map.md) for
the actual shared interfaces.

For target checks, inspect `tests/test_admin_inference_allowance_persistence.py`,
`tests/test_inference_master_api.py`, `tests/test_inference_master_runtime.py`,
`tests/test_model_pacing.py`, `tests/test_model_settings.py`,
`tests/test_admin_model_scheduling_api.py`,
`apps/omlx-web/tests/inference_scheduling_ui.test.cjs`,
`apps/omlx-web/tests/test_admin_model_settings_template.py`, and
`apps/omlx-web/tests/test_admin_usage_ui.py`. Check multiplication and zero
allowances, saved versus effective values, parent scaling, successful and
failed-save behavior, bypass/re-enable, migration backups and failures, profile
independence, and browser model selection/hide state. These tests are references
for qualification; they do not qualify a standalone allowance patch.

Return to the [catalog](../FORK_FEATURES.md) or [composition guide](../FEATURE_COMPOSITION.md).

[Open the fixed implementation source](https://github.com/actualrealhuman/omlx/tree/bd850747e6336ee59e6fc9242a865eda5257d061).
