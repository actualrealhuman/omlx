# Per-model generation concurrency limits

Feature ID: `per-model-concurrency`

## Behavior and use

Set a model-specific cap on simultaneous generation requests without changing
the global scheduler limit or other models' limits. The default is inherited
behavior: `max_concurrent_requests: null` uses the global scheduler value. A
model override must be a positive integer. The model settings editor exposes
the value in both the admin web UI and macOS app. Clear the override to return
to the global limit.

The cap applies to local batched LLM and VLM generation. It bounds both
scheduler admission (`max_num_seqs`) and mlx-lm's active decode batch
(`completion_batch_size`). The batched fallback used by DFlash follows the same
local scheduler path. Distributed deployments use the remote worker's scheduler
settings. This setting does not control embedding, reranking, audio, or
diffusion batching.

## Source and adoption boundary

- Availability: local focused branch; public availability of the implementation
  commit has not been verified.
- Source: `feature/per-model-concurrency`, commit
  `f519ae699db4d903f8828b0db4dcdbcbed653a01`.
- Baseline: `bd1e9b112474862ebed550a9c83b4316038a1d74` (the pinned stable
  `v0.7.1` release). The implementation commit's parent is
  `d6b2b92b11ebcbf4a4c1f002b0d590a3117212a8`, an ancestor of that baseline.
  The patch is one commit; its history contains no unrelated feature commits.
- Required changes: take the complete implementation commit, including
  `omlx/model_settings.py`, `omlx/model_profiles.py`, `omlx/engine_pool.py`,
  the admin routes, dashboard JavaScript and model settings template, all
  `omlx/admin/i18n/*.json` edits, macOS model settings/profile editor and DTO
  changes, and all tests and documentation changed by the commit. The complete
  changed-file list is the commit diff; do not take only the scheduler hook or
  setting field.
- Dependencies: no separate feature ID. It uses the existing per-model
  settings/profile persistence, admin model-settings API, and batched engine
  scheduler. Shared-file overlap with other scheduler changes is not itself a
  runtime dependency; port onto the chosen target and resolve those hunks
  against its current scheduler implementation.

## Validation

Suggested focused check:

```sh
python -m pytest tests/test_engine_pool.py tests/test_model_settings_profiles.py tests/test_admin_model_settings.py tests/test_admin_profiles_api.py tests/test_admin_model_settings_template.py -q
```

The tests cover positive-integer validation, clearing through the API with
`null`, profile round trips, independent per-engine scheduler configuration,
and reload when the effective cap changes. Also run the macOS
`ModelSettingsScreenVMTests` in `apps/omlx-mac/oMLX.xcodeproj` to cover the
native editor/DTO path. These are suggested checks, not recorded results. The
branch implementation has not been qualified on the pinned baseline with only
its declared prerequisites, and no tests are claimed here.

## Limitations and remaining work

An explicit cap changes request admission and decode batching for the selected
model; it does not promise higher throughput or lower latency. Pinned model
reload behavior and mixed model workloads should be checked on the intended
target. Port the single source commit to the adopter's chosen baseline, run the
listed checks there, and update this recipe with the resulting exact target
revision and qualification evidence before calling the source independently
adoptable.
