# Request-scoped cache retention

- **Feature ID:** `request-cache-policy`
- **Status:** Prepared source; target qualification is pending.
- **Baseline:** oMLX `v0.7.1`, commit `bd1e9b112474862ebed550a9c83b4316038a1d74`.
- **Implementation source:** commit `e3453b4bc6cfaec1f644b92be5cc81d900c2fc40`.
- **Dependencies:** None beyond the stated baseline and its normal runtime dependencies.
- **Public availability:** This source snapshot is local until its branch is separately published.

## Behavior

Generation callers can independently choose `default`, `memory`, or `none`
retention for reusable KV state and image features. They can provide choices in
the `cache_policy` request object or the `X-oMLX-KV-Cache` and
`X-oMLX-Image-Cache` headers. Successful supported responses acknowledge both
effective choices in those headers. Omitted settings preserve the server's
existing behavior.

`memory` permits compatible reuse and best-effort RAM retention within the
existing cache budget, without granting disk persistence. `none` prevents new
reusable retention for that cache component while retaining request-local
inference buffers. The policy does not purge prior entries, revoke another
request's persistence grant, reserve RAM, or provide request isolation or
secure erasure. It is separate from conversation storage, Responses `store`,
logs, media temporary files, model files, compiler caches, and OS swap.

The extension supports local batched text and VLM engines, including supported
VLM image and video feature caches. Engines that cannot enforce an explicit
restriction reject it before preprocessing or inference. Distributed,
diffusion, Splash, and other engine families need separate qualification before
they can accept restrictive policy values.

## Source boundary

The implementation adds the immutable policy and request/API contract, threads
the choice through local generation and scheduler cache paths, gates KV and
image-feature persistence, handles speculative and auxiliary cache paths, and
includes regression coverage. The main source areas are:

- `omlx/cache/policy.py`, `omlx/api/cache_policy.py`, API request schemas and
  response handling;
- `omlx/request.py`, `omlx/server.py`, `omlx/engine/`, `omlx/engine_core.py`,
  `omlx/scheduler.py`, and `omlx/specprefill/`;
- `omlx/cache/paged_ssd_cache.py`, `omlx/cache/prefix_cache.py`,
  `omlx/cache/vision_feature_cache.py`, and
  `omlx/cache/boundary_snapshot_store.py`;
- focused tests in `tests/test_cache_policy_api.py`,
  `tests/test_scheduler_cache_policy.py`, `tests/test_engine_cache_policy.py`,
  cache/prefix/vision/request/spec-prefill tests, and the packed-Affine cases in
  `tests/test_affine4_prefix_cache.py`.

The implementation is kept as one source feature because its API promise relies
on coordinated behavior across request parsing, supported engine paths, cache
reuse, eviction, asynchronous writes, speculative state, and response
acknowledgements. The internal modules are not independent adoption units.
This source snapshot also includes the stable-path hot-cache metadata identity
guard for queued writer completion. Stable 0.7.1 already forwards policy through
its direct synchronous generation path; later controlled-worker changes from
other branches are not part of this feature.

User behavior and limitations are documented in [`../cache-policy.md`](../cache-policy.md).
The persistence invariants and tradeoffs are recorded in
[`../decisions/request-cache-persistence.md`](../decisions/request-cache-persistence.md).

## Adoption and validation

Start from the exact baseline above and apply source through the implementation
commit. Keep all implementation, tests, and documentation together. Do not
copy private build manifests or integration history. Run the focused suite on
the target checkout:

```sh
python -m pytest -q \
  tests/test_cache_policy_api.py \
  tests/test_engine_cache_policy.py \
  tests/test_scheduler_cache_policy.py \
  tests/test_paged_ssd_cache.py \
  tests/test_prefix_cache.py \
  tests/test_prefix_cache_gdn_split.py \
  tests/test_vision_feature_cache.py \
  tests/test_vlm_cached_vision_inputs.py \
  tests/test_request.py \
  tests/test_specprefill_draft.py \
  tests/test_specprefill_target.py \
  tests/test_affine4_prefix_cache.py
```

Also run `tests/test_boundary_snapshot_store.py`, `tests/test_engine_core.py`,
`tests/test_scheduler.py`, `tests/test_vlm_engine.py`, and
`tests/test_distributed_engine.py` for the directly affected helper and fallback
paths. Tests should use synthetic inputs and temporary cache directories; they
do not establish real-model behavior, packaged-build behavior, or performance.
Those checks remain pending on this snapshot. Record the exact target revision,
test results, working-tree fingerprint if dirty, and any unsupported engine
families before describing the feature as qualified.

### Focused local validation

At source revision `1290c82fca51eaacebaccfd37dfa896a27fc94a2`, the full focused
and listed fallback/helper suites passed with pytest's default exclusions for
`slow` and `integration` tests. The local test environment used Python 3.11,
pytest 9.1.1, mlx-lm `0.31.4.dev132+g94cdcae13` (matching the pinned commit),
mlx-vlm 0.7.7, and MLX 0.32.3. This checkout pins MLX 0.32.4, so the exact MLX
patch-version remains a qualification limitation.

- Focused recipe command: 583 passed in 6.75 seconds.
- Directly affected helper/fallback command: 744 passed, 1 skipped in 7.09
  seconds.
- No real models, downloads, network requests, or benchmarks were used.

The candidate source revision remained unchanged during testing. These results
cover the declared synthetic/local test suites; they do not establish real
model behavior, packaged-build behavior, or performance. Re-run on the exact
pinned dependency set before removing the patch-version qualification note.
