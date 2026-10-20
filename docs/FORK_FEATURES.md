# Personal oMLX feature sources

Choose an addition, inspect its source, and follow the
[integration guide](INTEGRATING_FEATURES.md). The entries below identify public
code, including older implementations. They are not a menu of automatically
composable patches or a promise of compatibility with your upstream version.

Source references were checked on 2026-10-10. Fixed revisions make an entry
reproducible even when a branch moves. A baseline is the common upstream
ancestor used to inspect the topic diff; the version shown is the version
declared there, not a claim that the ancestor is a release tag. Validation
pointers tell you what to rerun after adapting a change, not that this guide
has retested every source snapshot.

## Combined inference controls

| Addition | Source snapshot | Baseline | Scope and integration notes |
| --- | --- | --- | --- |
| Combined inference controls | [feature/inference-controls][inference-controls-branch], source snapshot [bd850747][inference-controls-candidate] | Current upstream snapshot [73322ed1][base-inference-controls] | Combines Inference Throttle, battery protection, model-turn scheduling, hierarchical global/class/model allowances, the persisted master bypass, and accepted dashboard chart and model selection. Read its [user guide][inference-controls-guide] and [validation notes][inference-controls-tests]. The separate [Inference Throttle source][throttle] remains available and is not superseded. |

The combined scheduler is cooperative and covers supported local batched text
and VLM work. Percentages are configured relative allowances, not observed
utilization, watts, or GPU utilization; runtime status is reported separately.
Strict interactive priority can starve background work while interactive demand
continues. Results depend on the model, workload, machine, and surrounding load;
no universal performance improvement is promised.

**Local validation on 2026-10-20** for source snapshot `bd850747`:

| Check | Result |
| --- | --- |
| Static review and web assets | Source/history review and cached web generation passed. Native extensions and Metal libraries compiled and imported successfully; native sources are unchanged at this snapshot. |
| Focused suites | Python: 1,293 passed in 22.36 seconds. Node tests passed. |
| Default suite | 18,662 passed, 5 failed, 239 skipped, 85 deselected, 36 warnings in 412.08 seconds. All five failure IDs match the pristine upstream baseline; no additional failures were found. The suite is not green. |
| API-only fallback | With `apps/` absent, `omlx_web` was undiscoverable. 18,469 of 18,551 tests were selected (82 deselected); `server-main` passed 11 tests in 3.28 seconds. No actual HTTP health-server check was included. |
| Two-model functional check | Bounded Qwen3.5 0.8B and 9B VLM batched-engine check passed: both models remained resident; overlapping interactive/background requests completed under the coordinator; cancellation and resume produced valid outputs with clean handoff and cleanup. This is not a performance or energy benchmark. |

These results do not establish broad model/hardware coverage, performance or
energy outcomes, an HTTP health check without app packages, or PR readiness. If
the source changes, review and update the fixed source links and validation
record for the new revision.

## Published topic branches

| Addition | Public source snapshot | Source baseline | Integration notes |
| --- | --- | --- | --- |
| Inference Throttle | [feature/inference-throttle][throttle-branch], [ee978a1c][throttle] | 0.7.0 / [25aebb3e][base-throttle] | Live target work-share control for supported local batched generation, with a full-speed bypass at 100%. Includes both the initial feature and the adaptive MTP timing fix. Carry settings, scheduler ownership, API, UI, translations, and tests together. This is not a GPU-utilization or electrical-power cap; see its [scope note][throttle-scope]. |
| Battery and power controls, original implementation | [feature/battery-power-management][battery-branch], [3073fc3f][battery] | 0.6.4 / [ebf4f1fa][base-battery] | Battery telemetry plus the original pause/charge-recovery governor. This older branch actively gates inference. Extract telemetry separately if that is all you want; do not assume it matches a later personal build's behavior or composes with Inference Throttle. See [power-management design][battery-design]. |
| Cache inspection sidecars | [feature/cache-inspection-sidecars][cache-branch], [badc6715][cache] | 0.6.4 / [e008a66b][base-cache] | Opt-in token-ID and text companions for persisted SSD cache blocks. Includes storage lifecycle, scheduler capture, settings, and UI. Sidecars expose cached content; read the [format and lifecycle notes][cache-design]. |
| Live dashboard context usage | [feature/dashboard-context-usage][context-branch], [260c66fd][context] | 0.6.4 / [e467261e][base-ui] | Exposes in-progress context usage through request, engine, API, and dashboard paths. Preserve current engine accounting and inspect interactions with newer context limits. |
| Autosizing message editor | [feature/chat-edit-autosize][autosize-branch], [20dba1ca][autosize] | 0.6.4 / [e467261e][base-ui] | Inline chat editing grows with its content. Shares `chat.html` with the other chat changes; adapt to your current editor lifecycle. |
| Explicit edit cancellation | [feature/chat-edit-safe-cancel][cancel-branch], [c26317ff][cancel] | 0.6.4 / [b908f563][base-cancel] | Requires an explicit action to cancel a message edit. Keep focus, keyboard, and save behavior consistent with your target UI. |
| Preserve interrupted output | [feature/chat-preserve-interrupted-output][interrupted-branch], [86fd1d63][interrupted] | 0.6.4 / [e467261e][base-ui] | Keeps partial assistant output after stream failure. Reconcile with your chat persistence and cancellation behavior; bring its translation keys along. |
| High-contrast dark theme | [feature/high-contrast-dark-theme][theme-branch], [183625ff][theme] | 0.7.0.dev2 / [b390b31e][base-theme] | True-black admin/chat theme with stronger contrast. Uses shared CSS and templates; check dashboard, chat, login, and light appearance on your target. |
| Skip community benchmark upload, original patch | [patch/telemetry-upload-disabled][uploads-branch], [e2e9e189][uploads] | 0.6.4 / [e008a66b][base-cache] | The original patch skips the benchmark upload path. It is smaller than the later settings-based permission controls below; it does not represent a general network-access policy. |

## Sources inside the published combined build

These additions can be inspected in the fixed
[personal/main snapshot 45b0d7cb][combined], which declares version 0.7.0rc1.
It is an older integration snapshot, not the current local build. Its common
upstream ancestor is [3f2d07e8][base-combined]. A full diff from that ancestor
includes many unrelated features; use the entry's files, commits, and design
notes to identify the selected behavior.

| Addition | Source to inspect | Dependencies and validation pointers |
| --- | --- | --- |
| Chat-history storage safety | [Original change 2e4df089][history], [design][history-design], and the combined snapshot's `chat_history_storage.js` and `chat.html` | Preserve existing history on quota, serialization, or corrupt-data failures. The original change starts at [8288884d][base-history] (declared 0.7.0.dev4). It precedes the IndexedDB redesign. Inspect `tests/chat_history_storage.test.cjs` and `tests/test_chat_ui_overhaul.py`. |
| IndexedDB chat storage and media preservation | [Storage design][storage-design], `omlx/admin/static/js/chat_indexeddb_store.js`, `chat_history_storage.js`, `chat_history_migration.js`, and `chat.html` in the [combined snapshot][combined] | Extends storage safety with asynchronous records, migration, revision coordination, and media persistence. The old design's publication-status prose is historical; the code is accessible in this snapshot. Extract the storage stack together rather than treating its last translation commit as the feature. Inspect the chat record, IndexedDB, history/backend migration, media, and app-integration `.test.cjs` suites. The design describes deferred work; do not infer that every phase is implemented. |
| Settings-based benchmark upload permission | `omlx/settings.py`, `omlx/admin/benchmark.py`, `omlx/admin/accuracy_benchmark.py`, routes, and settings UI in the [combined snapshot][combined] | Master permission defaults off, with separate throughput and accuracy choices. Later development of the original skip-upload patch. Inspect benchmark, accuracy benchmark, settings, and `test_benchmark_upload_settings_ui.py` tests; check each upload entry point. |
| Supervisor-owned web restarts | [Change 5878dc7d][restarts], starting at [1efcb1f9][base-restarts] (declared 0.7.0.dev2) | Coordinates web restart requests with the native supervisor. The Swift process lifecycle and Python route must agree. Inspect `tests/test_admin_restart.py` and native `ServerProcessIntegrationTests.swift` at the source revision, then port to your current restart architecture. |
| Local app activation and restart checks | [Change 02dd8ddf][activation], starting at [5878dc7d][restarts] | Adds `apps/omlx-mac/Scripts/activate_build.py` and `verify_restart.py`. Built on the supervisor-owned restart change. These helpers can launch or control an app; read their behavior and use your own installation procedure before running them. |

## Validation starting points

The paths here belong to the linked feature snapshots. Inspect their contents
and your target's tools before running them. A Python test file can exercise
several unrelated cases; select additional engine, settings, and UI checks as
needed for your integration.

| Topic source | Checks to inspect and adapt |
| --- | --- |
| Inference Throttle | `tests/test_inference_pacing.py`, `test_admin_inference_throttle_ui.py`, `test_mlx_lm_mtp_patch.py`, engine/core, settings, and prefill regression tests. Read [the source testing notes][throttle-tests]. Check cancellation, multiple engines, 100% bypass, and MTP timing on the target. |
| Combined inference controls | Scheduler, battery, allowance, settings/API, dashboard, text/VLM, and migration checks are described in the source snapshot's [validation notes][inference-controls-tests]. See the dated validation summary above; it includes five default-suite failures matching the pristine upstream baseline and a bounded two-model functional check. No broad performance or energy result is claimed. |
| Original battery controls | `tests/test_power_sources.py`, `test_power_management.py`, `test_engine_core.py`, `test_decode_fairness.py`, and settings tests. Inspect the governor's assumptions before real battery/power-transition checks. |
| Cache inspection | `tests/test_inspection.py`, `test_inspection_files.py`, `test_cache_inspection_integration.py`, settings and VLM tests. Check disabled behavior, creation, eviction, and cache restoration. |
| Live context | Request, engine/core, batched/VLM/DFlash, context-window, and active-model visibility tests changed by the source commit. |
| Chat editor changes | `tests/test_chat_ui_overhaul.py`; also exercise multiline editing, keyboard/focus behavior, and persistence in the target browser UI. |
| Interrupted output | `tests/test_chat_stream_cancellation.py`, plus your target's persistence tests and a stream-interruption UI check. |
| Dark theme | `tests/test_admin_dark_theme.py` plus visual checks of affected screens in dark and light appearance. |
| Original upload patch | `tests/test_benchmark.py`; verify the upload is skipped and local benchmark results remain usable. |

Keep entries focused on code that is actually available. A public branch, a
feature label, or a historical test result does not prove compatibility with
someone else's checkout. See the [source-sharing scope](decisions/fork-source-sharing.md).

[throttle-branch]: https://github.com/actualrealhuman/omlx/tree/feature/inference-throttle
[throttle]: https://github.com/actualrealhuman/omlx/tree/ee978a1c466323aa366f82bb831e99fe423583ea
[base-throttle]: https://github.com/actualrealhuman/omlx/commit/25aebb3ee0bb052a264241953bef6820f102dd0b
[throttle-scope]: https://github.com/actualrealhuman/omlx/blob/ee978a1c466323aa366f82bb831e99fe423583ea/docs/decisions/inference-throttle.md
[throttle-tests]: https://github.com/actualrealhuman/omlx/blob/ee978a1c466323aa366f82bb831e99fe423583ea/docs/TESTING.md
[inference-controls-branch]: https://github.com/actualrealhuman/omlx/tree/feature/inference-controls
[inference-controls-candidate]: https://github.com/actualrealhuman/omlx/tree/bd850747e6336ee59e6fc9242a865eda5257d061
[inference-controls-guide]: https://github.com/actualrealhuman/omlx/blob/bd850747e6336ee59e6fc9242a865eda5257d061/docs/inference-controls.md
[inference-controls-tests]: https://github.com/actualrealhuman/omlx/blob/bd850747e6336ee59e6fc9242a865eda5257d061/docs/TESTING.md#inference-controls
[base-inference-controls]: https://github.com/actualrealhuman/omlx/commit/73322ed188a25b9ce2e490d3137d810f7d50128d
[battery-branch]: https://github.com/actualrealhuman/omlx/tree/feature/battery-power-management
[battery]: https://github.com/actualrealhuman/omlx/tree/3073fc3f773b0ccfdd4bf39c3c5f1adaf94bb186
[base-battery]: https://github.com/actualrealhuman/omlx/commit/ebf4f1faaf7791fb073a220ddf713cfd90caa0b7
[battery-design]: https://github.com/actualrealhuman/omlx/blob/3073fc3f773b0ccfdd4bf39c3c5f1adaf94bb186/docs/power-management.md
[cache-branch]: https://github.com/actualrealhuman/omlx/tree/feature/cache-inspection-sidecars
[cache]: https://github.com/actualrealhuman/omlx/tree/badc6715af92e75285cb9c0f947e5c08f7e49bf9
[base-cache]: https://github.com/actualrealhuman/omlx/commit/e008a66b4703bc77404dab30f8f898a117d49dfe
[cache-design]: https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/docs/cache-inspection.md
[context-branch]: https://github.com/actualrealhuman/omlx/tree/feature/dashboard-context-usage
[context]: https://github.com/actualrealhuman/omlx/tree/260c66fd7086677dd314aee9e495c56a939e38b6
[base-ui]: https://github.com/actualrealhuman/omlx/commit/e467261edc786efd33b1e9023d5c4a827f8aa1c1
[autosize-branch]: https://github.com/actualrealhuman/omlx/tree/feature/chat-edit-autosize
[autosize]: https://github.com/actualrealhuman/omlx/tree/20dba1ca9c2a128eae63439bc80268f7c40e1507
[cancel-branch]: https://github.com/actualrealhuman/omlx/tree/feature/chat-edit-safe-cancel
[cancel]: https://github.com/actualrealhuman/omlx/tree/c26317ff05ed779c0056aefc81bb80ae3fb97821
[base-cancel]: https://github.com/actualrealhuman/omlx/commit/b908f563c32378f6479a093093fb988b76e05b7c
[interrupted-branch]: https://github.com/actualrealhuman/omlx/tree/feature/chat-preserve-interrupted-output
[interrupted]: https://github.com/actualrealhuman/omlx/tree/86fd1d6386849535f83834458b13c708ff0849a7
[theme-branch]: https://github.com/actualrealhuman/omlx/tree/feature/high-contrast-dark-theme
[theme]: https://github.com/actualrealhuman/omlx/tree/183625ffa310193549ddc961533caae3f84aa07d
[base-theme]: https://github.com/actualrealhuman/omlx/commit/b390b31e0c6831225fed0f24d278eb1db7fcb68b
[uploads-branch]: https://github.com/actualrealhuman/omlx/tree/patch/telemetry-upload-disabled
[uploads]: https://github.com/actualrealhuman/omlx/tree/e2e9e1899fd62b94544428548255136231573189
[combined]: https://github.com/actualrealhuman/omlx/tree/45b0d7cbf753ba73b466a51620d71a8b1f9fa7ed
[base-combined]: https://github.com/actualrealhuman/omlx/commit/3f2d07e8dff257119329e0a2e9821df81182f05d
[history]: https://github.com/actualrealhuman/omlx/commit/2e4df0898aef4502d02e745bb8e241c3a1677a84
[base-history]: https://github.com/actualrealhuman/omlx/commit/8288884d9b4f6db7b547633a94d36794c6b1d52d
[history-design]: https://github.com/actualrealhuman/omlx/blob/45b0d7cbf753ba73b466a51620d71a8b1f9fa7ed/docs/decisions/0001-chat-history-storage-safety.md
[storage-design]: https://github.com/actualrealhuman/omlx/blob/45b0d7cbf753ba73b466a51620d71a8b1f9fa7ed/docs/decisions/0002-chat-storage-redesign.md
[restarts]: https://github.com/actualrealhuman/omlx/commit/5878dc7da5630c6365ef15c0219f8a9daabb1946
[base-restarts]: https://github.com/actualrealhuman/omlx/commit/1efcb1f91ad395179547cd48c7203dd2b03dfa96
[activation]: https://github.com/actualrealhuman/omlx/commit/02dd8ddf52b904ce90ec3f428ad6e12ed33f0c07
