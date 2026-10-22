# Inference-control source map

This map distinguishes the published combined source bundle from components
that are visible in its code but have not been independently extracted and
qualified. It is an adoption guide for source, not a build or release recipe.

## Fixed snapshots

The combined inference-controls source is `bd850747e6336ee59e6fc9242a865eda5257d061`,
based on `73322ed188a25b9ce2e490d3137d810f7d50128d`. It contains global and
per-model throttling, hierarchical allowances, serial model turns, the master
bypass, battery reserve protection, and the web controls. Treat this combined
diff as the adoption unit unless you are doing the component extraction and
qualification work described below.

Historical validation at this exact source revision was recorded on
2026-10-20. The default suite had five numerical failures that also reproduced
on the exact `73322ed1` upstream baseline. A bounded two-model functional check
was recorded. This history does not establish broad model/hardware coverage,
performance or energy savings, or compatibility with another target. See the
[source validation notes](https://github.com/actualrealhuman/omlx/blob/bd850747e6336ee59e6fc9242a865eda5257d061/docs/TESTING.md#inference-controls).

The standalone throttle is a separate source alternative: `ee978a1c466323aa366f82bb831e99fe423583ea`,
based on `25aebb3ee0bb052a264241953bef6820f102dd0b`. It covers shared pacing
without battery protection, serial turns, or the later hierarchical allowance
panel. Choose one implementation; do not layer both pacers into one engine.

## Runtime components and real seams

| Concern | Main source and live boundary | Required neighboring code |
| --- | --- | --- |
| Work-share pacing | `InferencePacer` in `omlx/inference_pacing.py`; `ModelPacer` in `omlx/model_pacing.py`; `EngineCore._acquire_execution_pacing()` / `_finish_execution_pacing()` and batched/VLM step hooks | `EnginePool` ownership, engine config, scheduler activity accounting, settings and authenticated admin updates. The standalone throttle is a separate implementation snapshot. |
| Model turns | `ModelExecutionCoordinator`, `ModelRegistration`, `ModelWork`, `ModelSubmission` in `omlx/inference_scheduling.py`; engine registration, `_execution_context()` and `_drain_model_execution()` in `engine_core.py`; scheduler execution checkpoints | `EnginePool` model lifecycle/admission, safe preparation/decode boundaries, priority/settings/status API, shared pacing and cancellation/cleanup paths. A coordinator file alone does not provide safe ownership. |
| Battery policy | `BatteryProtectionSettings` / `BatteryPolicy.evaluate()` in `omlx/battery_protection.py`; `BatteryProtectionService._evaluate()` and `.sample_now()` in `omlx/battery_service.py` | `PowerSourceReader` and notifier, `GenerationGate`, `EnginePool` share application, `EngineCore`/scheduler gate checks, settings persistence, admin status/configuration and UI. The policy composes below user limits and can suspend. |
| Power observation | `PowerSourceReader.sample()` and `PowerSourceChangeNotifier` in `omlx/utils/power_sources.py`; `BatteryProtectionService._read_sample()` converts the platform snapshot to `BatterySample` | In the current combined source, observation and action are joined by `BatteryProtectionService`. There is no `telemetry_only` switch or independently qualified telemetry-only service. Extract a read-only service explicitly before advertising telemetry as an adoption unit. |
| Allowances and master bypass | `EnginePool.configure_inference_share()`, `configure_inference_allowances()`, `configure_model_scheduling()` and `_apply_model_policy()`; `ServerSettings` and `ModelSettings`; `ModelSettingsManager.migrate_inference_allowances()` | Admin settings schemas/routes and status serializers; model API/settings migration and backups; dashboard controller, templates, translations, and tests. A chart alone does not enforce a ceiling. |
| API and dashboard | `GlobalSettingsRequest`, model settings request schemas, `/api/inference-scheduling`, global settings PATCH and status serializers in `omlx/admin/routes.py`; `apps/omlx-web/omlx_web/static/js/dashboard.js`, `templates/dashboard/blocks/_inference_throttle.html`, `_inference_throttle_control.html`, `_settings.html`, `_status.html`, `_modal_model_settings.html`, `dashboard_layout.js`, CSS and `i18n/*.json` | Runtime policy above, localization, persistence and successful/failed-save behavior. The master switch save must be acknowledged before the UI changes confirmed state. |

The persisted global `ServerSettings` fields are `inference_share`,
`inference_interactive_allowance`, `inference_background_allowance`,
`inference_scheduling_enabled`, `inference_scheduling_quantum_seconds`,
`inference_controls_enabled`, `battery_protection_enabled` and the numeric
`battery_protection` options. Per-model `ModelSettings` uses
`inference_allowance`, `inference_priority` and `inference_exclusive`; the
one-time `ModelSettingsManager.migrate_inference_allowances()` converts older
absolute `inference_share` values and backs up the originals. `exclusive`
selects the Interactive class in this implementation; it is not a GPU-exclusive
lock.

The shared `GenerationGate` in `omlx/generation_gate.py` carries pause and
restricted state into async and synchronous work. It does not pace work by
itself. `InferencePacer` accounts for submission work and rest time. The model
turn coordinator arbitrates which model owns a cooperative execution turn.
Battery policy computes a stricter effective ceiling and can close the hard
gate. These roles overlap at engine checkpoints, but they are different
policies with different state and failure behavior.

## Adoption order

For the complete published feature, take the fixed combined source commit and
its tests, settings migration, API, dashboard, translations and docs together.
Do not reassemble it from file lists alone.

For a future component extraction, use this dependency order and create new
focused source revisions with their own recipes:

1. Choose a single pacing implementation and establish the engine/scheduler
   work accounting and cancellation contract. The standalone throttle can be
   adopted as its own alternative.
2. If needed, extract read-only host power observations with no policy or
   generation-gate side effects. The current combined service is not that
   extraction.
3. Extract either model turns or battery policy on top of the chosen pacing
   and execution boundaries. For battery protection, include telemetry,
   `GenerationGate`, effective-share composition, suspension and recovery.
   For turns, include model lifecycle, safe checkpoints, drains and handoff.
4. Add hierarchical allowances and the master bypass with settings migration,
   admin API/status, model policy, UI, localization and persistence behavior.
   The current chart relies on these runtime components.
5. Requalify the integrated selection on the stated baseline, including
   disabled/default behavior, cancellation, maintenance, supported text/VLM
   paths, translations and saved settings. Update the feature recipe and
   catalog with the exact source and target revisions.

This order is a seam map, not proof that the current branches at each seam are
already independent. The available supported adoption units are the combined
bundle and the separate standalone throttle snapshot; the other names are
bundle components until extraction and isolation qualification are complete.

## Runtime switches versus source removal

| Runtime control | What it bypasses | What remains present |
| --- | --- | --- |
| `inference_controls_enabled: false` | User global/class/model limits and serial-turn enforcement | Saved preferences, code and API/UI; battery limits and suspension remain active. |
| `inference_scheduling_enabled: false` | Model-to-model turn arbitration | Pacing and configured global/class/model ceilings remain active. |
| `inference_share: 1.0` with allowances at `1.0` | Intentional pacing delay at the full-speed ceiling | Pacers, engine hooks and settings/API still exist. Battery policy may lower the effective ceiling or close the gate. |
| `battery_protection_enabled: false` | Battery curve, outage policy and reserve suspension | Sensor/service code, settings, API/status/UI and other inference controls remain in source. |
| Model allowance `0` | Admission for that covered model while controls are enabled | Model remains loaded; cancellation, cleanup and maintenance still need to work. |

Removing a feature from a source checkout requires removing its hooks and
state, settings and migration fields, admin schema/routes/status, dashboard
controller/templates/translations, and feature-owned tests/docs, then checking
for consumers and interactions. A switch that makes a component idle is not a
source extraction.

## Concrete extraction checklist: telemetry without a governor

The current service has no telemetry-only mode: `BatteryProtectionService`
calls `BatteryPolicy.evaluate()`, applies effective shares, closes
`GenerationGate`, persists recovery state and may suspend requests. To prepare
a telemetry-only feature:

1. Keep the platform boundary in `PowerSourceReader.sample()` and
   `PowerSourceChangeNotifier`; define a stable, immutable sample/status
   contract and stale/error behavior.
2. Add a collector/reporting service that samples and reports only. Do not
   construct `BatteryProtectionService`, `BatteryPolicy`,
   `GenerationGate`, `InferencePacer`, or the historical `BatteryPowerManager`.
3. Wire a read-only status endpoint or observer to the collector. Verify that
   sampling cannot change shares, scheduler admission, generation pauses or
   request state.
4. Include platform-unsupported, absent battery, missing/stale data, sampling
   errors, notifier fallback and cleanup tests. Add a negative assertion that
   generation remains ungated while telemetry changes.
5. Assign a stable feature ID and state whether it replaces the historical
   `battery-power` contract. Record the exact baseline/source, adoption recipe
   and isolated qualification. Do not label the current combined service
   telemetry-only or infer watts saved from charge observations.

## Component recipes

- [Combined inference controls](features/inference-controls.md) — published
  bundle and historical qualification.
- [Standalone inference pacing](features/inference-pacing.md) — separate
  throttle alternative.
- [Battery reserve protection](features/battery-protection.md) — coupled
  bundle component.
- [Model execution turns](features/inference-scheduling.md) — coupled bundle
  component.
- [Hierarchical allowances](features/inference-allowances.md) — policy, API
  and dashboard component with migration requirements.
- [Historical battery power management](features/battery-power.md) — older
  active governor and a future telemetry-only extraction checklist.
