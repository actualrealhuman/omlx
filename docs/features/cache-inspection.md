# Inspect persisted cache content

Feature ID: `cache-inspection` · Availability: **Topic**

## Behavior and use

Optionally writes exact token IDs and annotated text beside persisted SSD KV blocks, so cached content can be identified. Capture defaults off.

Enable Save Cache Inspection Files in advanced cache settings, pass `--cache-inspection`, or set `OMLX_CACHE_INSPECTION=true`. Read the source's [format and lifecycle guide](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/docs/cache-inspection.md).

## Source and adoption boundary

- Source: [`badc6715af92e75285cb9c0f947e5c08f7e49bf9`](https://github.com/actualrealhuman/omlx/tree/badc6715af92e75285cb9c0f947e5c08f7e49bf9) on `feature/cache-inspection-sidecars`.
- Baseline: [`e008a66b4703bc77404dab30f8f898a117d49dfe`](https://github.com/actualrealhuman/omlx/commit/e008a66b4703bc77404dab30f8f898a117d49dfe).
- These are fixed source snapshots, not a promise of compatibility with your target.

Carry token capture from request/scheduler paths, inspection rendering, SSD sidecar creation/accounting/eviction, configuration and UI together. Inspection files are not inputs to cache restoration.

Dependencies and interactions: Requires the target's paged SSD cache interfaces, not another catalog addition. When combining with request retention policies, restrictive requests must not grant sidecar persistence or backfill.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`omlx/cache/inspection.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/omlx/cache/inspection.py)
- [`omlx/cache/inspection_files.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/omlx/cache/inspection_files.py)
- [`omlx/cache/paged_ssd_cache.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/omlx/cache/paged_ssd_cache.py)
- [`omlx/cache/prefix_cache.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/omlx/cache/prefix_cache.py)
- [`omlx/scheduler.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/omlx/scheduler.py)
- [`omlx/request.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/omlx/request.py)
- [`omlx/settings.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/omlx/settings.py)
- [`omlx/admin/routes.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/omlx/admin/routes.py)

## Validation

Check capture disabled, new capture, opportunistic backfill, write failures, block eviction/clear, orphan cleanup, and restoration with missing or corrupt sidecars. Use synthetic prompt content and a temporary cache.

Checks to inspect and adapt from the fixed source:

- [`tests/test_inspection.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/tests/test_inspection.py)
- [`tests/test_inspection_files.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/tests/test_inspection_files.py)
- [`tests/test_cache_inspection_integration.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/tests/test_cache_inspection_integration.py)
- [`tests/test_settings.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/tests/test_settings.py)
- [`tests/test_vlm_engine.py`](https://github.com/actualrealhuman/omlx/blob/badc6715af92e75285cb9c0f947e5c08f7e49bf9/tests/test_vlm_engine.py)

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

Sidecars expose recoverable prompt content. Turning capture off does not remove existing files. Main KV files and sidecars are individually atomic, not a multi-file transaction. The older scheduler and web paths need adaptation on newer oMLX.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).

