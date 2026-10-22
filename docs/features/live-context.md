# See context usage while requests run

Feature ID: `live-context` · Availability: **Topic**

## Behavior and use

Carries in-progress context usage through request/engine status into the active-model dashboard, instead of relying only on completed request totals.

After integration, open the dashboard's active-model status while a request is running. There is no separate opt-in setting in this topic change.

## Source and adoption boundary

- Source: [`260c66fd7086677dd314aee9e495c56a939e38b6`](https://github.com/actualrealhuman/omlx/tree/260c66fd7086677dd314aee9e495c56a939e38b6) on `feature/dashboard-context-usage`.
- Baseline: [`e467261edc786efd33b1e9023d5c4a827f8aa1c1`](https://github.com/actualrealhuman/omlx/commit/e467261edc786efd33b1e9023d5c4a827f8aa1c1).
- These are fixed source snapshots, not a promise of compatibility with your target.

Carry request accounting, engine status, server/admin serialization and dashboard rendering together. A dashboard-only transplant would omit the live values.

Dependencies and interactions: No other catalog addition is required. Request accounting and context-window enforcement overlap with newer upstream work; preserve those interfaces.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`omlx/request.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/omlx/request.py)
- [`omlx/engine_core.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/omlx/engine_core.py)
- [`omlx/engine/batched.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/omlx/engine/batched.py)
- [`omlx/engine/dflash.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/omlx/engine/dflash.py)
- [`omlx/engine/vlm.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/omlx/engine/vlm.py)
- [`omlx/server.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/omlx/server.py)
- [`omlx/admin/routes.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/omlx/admin/routes.py)
- [`omlx/admin/templates/dashboard/_status.html`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/omlx/admin/templates/dashboard/_status.html)

## Validation

Observe accounting during prefill/decode, completion and cancellation, including cached prefixes and multiple active requests. Verify values do not become a second independent source of context-limit enforcement.

Checks to inspect and adapt from the fixed source:

- [`tests/test_active_models_visibility.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/tests/test_active_models_visibility.py)
- [`tests/test_request.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/tests/test_request.py)
- [`tests/test_context_window.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/tests/test_context_window.py)
- [`tests/test_engine_core.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/tests/test_engine_core.py)
- [`tests/test_batched_engine.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/tests/test_batched_engine.py)
- [`tests/test_dflash_engine.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/tests/test_dflash_engine.py)
- [`tests/test_vlm_engine.py`](https://github.com/actualrealhuman/omlx/blob/260c66fd7086677dd314aee9e495c56a939e38b6/tests/test_vlm_engine.py)

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

The topic uses an older engine/status schema. Compare target behavior first; similar upstream fields may already satisfy the requested outcome.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).
