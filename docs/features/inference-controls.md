# Combined inference controls

- **Bundle name:** `inference-controls` (not a separate build feature ID).
- **Adoption unit:** Published bundle.
- **Source:** `bd850747e6336ee59e6fc9242a865eda5257d061`.
- **Baseline:** `73322ed188a25b9ce2e490d3137d810f7d50128d`.
- **Status:** Bundle historically qualified on this baseline; compatibility with another target remains unverified.

This is the only directly documented adoption unit for the combined live
throttle, battery reserve protection, model turns, hierarchical allowances,
master bypass, settings migration and dashboard controls. Keep the runtime,
admin schemas/status, web templates/controller, translations, settings/model
migration, tests and docs together. Do not pick a subset based only on file
names or visible settings toggles.

The runtime seams and safe component order are in the
[inference adoption map](../inference-adoption-map.md). The fixed-source user
contract is in [Inference controls](../inference-controls.md).

The backend addition is primarily in `c7a866d3` (45 files); UI/localization in
`a45d791f` (23 files); documentation in `60b0e91a`; and restored dashboard
controller in `bd850747`. These commit boundaries do not imply that the
subsystems are independently installable. Runtime integration crosses
`omlx/inference_pacing.py`, `inference_scheduling.py`, `model_pacing.py`,
`battery_protection.py`, `battery_service.py`, `generation_gate.py`,
`engine_core.py`, `engine_pool.py`, `scheduler.py`, `settings.py`,
`model_settings.py`, `server.py` and `admin/routes.py`, with a wide dashboard
and test delta.

Historical qualification for source `bd850747` was recorded on 2026-10-20.
The default suite had five numerical failures also reproduced on baseline
`73322ed1`; a bounded two-model functional check was recorded. This does not
claim broad model/hardware qualification, a performance improvement or energy
savings. See [`../TESTING.md#inference-controls`](https://github.com/actualrealhuman/omlx/blob/bd850747e6336ee59e6fc9242a865eda5257d061/docs/TESTING.md#inference-controls)
and the [source map](../inference-adoption-map.md#fixed-snapshots).

For the separate throttle-only alternative, use the
[standalone pacing recipe](inference-pacing.md). Do not combine its separate
pacer implementation with this bundle.

## Prepared model-chooser layout follow-up

Local follow-up `d861a301e2630db33e41cfb95087e09adae6f43e` applies after the
published bundle above. It stacks each model name above its status beside a
checkbox, allowing both text rows to wrap in narrow dashboard widgets. Only
dashboard CSS and the user guide change; no additional runtime dependency,
setting or migration is introduced.

This follow-up is not yet published. Headless Chrome inspection on 2026-10-27
used a fixture with production dashboard CSS, theme, Tailwind and font assets.
It reproduced the old compression and confirmed stacked text at widget widths
of 180, 220, 280 and 600 CSS pixels, including long unbroken model identifiers,
translated status text and light/dark appearance. The existing clickable-label
markup and selection handlers are unchanged.

This was a browser layout inspection, not a live-server, real-device or runtime
regression suite. Historical bundle results above do not qualify the changed
source. Verify public availability before updating the published snapshot.

Return to the [catalog](../FORK_FEATURES.md) or [composition guide](../FEATURE_COMPOSITION.md).

[Open the fixed implementation source](https://github.com/actualrealhuman/omlx/tree/bd850747e6336ee59e6fc9242a865eda5257d061).
