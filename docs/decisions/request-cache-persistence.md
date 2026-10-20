# Per-request KV and image-feature cache retention

Status: Proposed; API names and shared-cache rules are a draft for review.
Date: 2026-10-20

## Problem and objective

Short-lived prompts can benefit from prefix reuse during a session without
being valuable enough to serialize to SSD. The existing global
`cache.hot_cache_only` setting applies to KV caching for every request. Ordinary hot-cache
write-back delays persistence; it does not prevent later eviction, clearing,
memory recovery or teardown from writing a block to disk.

Allow a client to request best-effort RAM caching or no new reusable cache
retention. Cover KV/recurrent state and the separate image-feature cache, with
independent choices for each. One-shot image title or metadata generation can
avoid retaining image features entirely. Preserve existing behavior for clients
that omit the option. This is a retention and write-volume control, not a
retention deadline, memory reservation, secure-erasure feature or privacy
boundary.

## Proposed client contract

Add this optional oMLX extension to generation requests:

```json
{
  "cache_policy": {
    "kv": "memory",
    "images": "none"
  }
}
```

Support the same choice as a request header for clients that can configure
custom headers but cannot add JSON fields:

```http
X-oMLX-KV-Cache: memory
X-oMLX-Image-Cache: none
```

| Requested value | Meaning |
| --- | --- |
| `default` | Follow the server's existing cache configuration. Persistence is permitted where the server permits it, not forced. |
| `memory` | Reuse compatible cached state and retain new entries in RAM if capacity permits. Do not grant disk-persistence permission. |
| `none` | Do not insert, extend, promote or newly retain reusable entries in RAM or on disk for the selected cache. Request-local inference buffers remain necessary. |

Both `kv` and `images` accept the same three values. Each omitted member means
`default`, unless its corresponding header supplies a value. Omitting the object,
passing a null object, or passing an empty object supplies no overrides. Reject
null members, unknown members, invalid values and non-object policies using the
endpoint's normal request validation error format. If a header and its explicit
JSON member disagree, reject the request before preprocessing or inference.
Duplicate header values must be validated together; conflicting or malformed
duplicates are invalid.

`kv` covers target/draft KV, recurrent state and their checkpoints. `images`
covers the separate vision-feature cache, including supported video features
that use the same cache. Each setting applies to all relevant inputs in that
request; per-image selection is outside the first version.

The example retains KV in RAM opportunistically but does not retain new image
features. A fully transient inference request uses `"kv": "none"` and
`"images": "none"`. Setting only `images` to `none` does not prevent image-derived
state being retained inside the request's KV; clients that want neither
representation retained must restrict both caches.

Request header names are case-insensitive; policy values use the documented
lowercase spelling. Body and header inputs normalize to one immutable internal
policy with two independently resolved components.

Apply the extension to `/v1/chat/completions`, `/v1/completions`,
`/v1/responses` and `/v1/messages`, including streaming and non-streaming
variants. The first implementation targets local batched text and VLM engines.
An engine that cannot honor an explicit restrictive choice must reject it before
starting work, rather than silently accepting it. Distributed, diffusion, Splash and
other engine families require separate qualification before accepting it.

For a successful request, return this acknowledgement before a streaming body
starts, or on the ordinary non-streaming response:

```http
X-oMLX-KV-Cache: memory
X-oMLX-Image-Cache: none
```

Each response value is `default`, `memory`, `none`, or `disabled`, describing
that component's effective policy. `none` acknowledges the client's no-retention
choice; `disabled` means that cache subsystem is unavailable or disabled by the
server. Neither promises a memory reservation. A disabled subsystem trivially
satisfies a restrictive request. With global KV RAM-only caching, a default KV
choice reports `memory`. Return both acknowledgements on successful participating
requests for consistent discovery. Browser clients need both response headers
exposed through the existing CORS policy.

A client requiring this contract must check the acknowledgement. Older oMLX
versions may ignore an unknown body field or header; successful generation alone
does not prove support. This extension is specific to oMLX, not an OpenAI or
Anthropic protocol guarantee.

## Effective policy and retention

Server configuration is an upper bound on what clients may enable:

| Server configuration | Request | Effective behavior |
| --- | --- | --- |
| Reusable KV caching disabled | Any KV choice | No new reusable KV retention or KV persistence. |
| Global hot-cache-only | `default` or `memory` for KV | RAM retention only, bounded by the configured budget. |
| SSD-backed caching | `default` | Existing RAM/SSD behavior, including configured write-through. |
| SSD-backed caching | `memory` | RAM retention only for new request-derived entries. |
| Any available cache | `none` for that component | No new reusable entries or request-granted persistence. |

Resolve the image-feature component against its own availability and existing
RAM/disk configuration. Its current setup does not consult the global KV
`hot_cache_only` flag. Preserve that existing behavior when `images` is omitted;
explicit `images: memory` must suppress its SSD enqueue regardless of KV settings.
One component's request choice does not change the other. A client wanting both
tiers RAM-only sends both `kv: memory` and `images: memory`.

`memory` never enables a disabled cache, allocates a new RAM budget, pins a
prefix, or guarantees a cache hit. If the hot-cache budget is zero, full, or an
entry is too large, generation can proceed without retaining that entry.
Request-local state needed for active inference remains subject to normal
memory admission. Evict reusable entries or recompute later; never substitute
an SSD spill for a prohibited write. Failure to retain reusable state is not a
generation error. Inability to fit active inference can still produce the
existing memory admission or runtime error.

`none` does not eliminate the tensors required to encode an image, perform
prefill or decode the current request. Keep those request-local, release their
references during normal cleanup, and do not transfer them into a reusable
cache, cache staging queue or writer. Do not disable image-feature computation
itself or force a second encoder pass merely to bypass retention.

The policy covers reusable state from prefill and generated continuation alike,
including cancellation and error cleanup. A follow-up request specifies its own
policy; conversation or response identifiers do not implicitly carry it forward.

## Reads and shared prefixes

`memory` may read a compatible prefix that already exists on SSD. It does not
delete existing files, cancel previously authorized writes, or downgrade entries
that another request has already made eligible for persistence. Reading an
SSD-backed entry must not cause a rewrite solely for the memory-only request.

`none` may reuse an already existing compatible entry without inserting or
promoting an entry, granting persistence permission or retaining a new reusable
copy on this request's behalf. Borrowed active-request references are allowed.
If the current read path necessarily populates a cache, add a non-populating
lookup or bypass that lookup. Neither restrictive mode purges entries or writes
already authorized by another request. Strict read bypass and erasure are outside
this feature.

Persistence permission belongs to the exact reusable cache artifact, not to the
last request that touched a shared manager:

1. An artifact first created only by `memory` work is not persistence-eligible.
   `none` work creates no reusable artifact and grants no persistence permission.
2. Merely finding or touching it does not grant permission. A default request
   may grant permission only after validating and actually adopting the exact
   compatible artifact for its own prefix reuse, or producing it itself.
3. Such a grant allows normal persistence of that artifact, including any
   checkpoint required to restore it. It does not grant permission to unmatched
   suffixes, sibling entries, or unrelated request snapshots.
4. A later `memory` request never revokes an existing grant. An existing grant
   never permits persistence of new descendants computed only by that request.
5. Global SSD restrictions still override an artifact's permission.

Apply equivalent permission rules to shared image-feature entries. A default
request may persist an exact feature entry it actually adopts, including one
previously retained only in RAM. A `memory` or `none` image request never makes
that grant. Touching an index during a rejected or unused lookup is insufficient.

For example, A creates a memory-only 4,096-token prefix. B later uses the first
2,048 tokens under `default`. Only the exact adopted prefix blocks and their
required checkpoints may become eligible; A's unused remainder stays RAM-only.
This proposal explicitly permits that sharing to preserve cache reuse and reduce
duplicate storage. Strict isolation from other requests would be a different
feature.

Permission promotion, replacement and eviction need atomic coordination. A
concurrent default request may legitimately authorize a shared artifact's write;
a memory-only request alone must never do so. Permission is not added to the
content hash or on-disk compatibility signature: policy does not change the
numerical identity of the cache. A promotion must retain access to the complete
artifact before claiming it is restorable or enqueueing a write.

## Covered persistence paths

The contract prohibits creating or rewriting these request-derived cache
payloads, including temporary and staging copies, unless the exact artifact has
an independent persistence grant:

- Target-model paged KV blocks, full and partial terminal blocks, and cache
  inspection sidecars associated with them.
- Prefill boundary snapshots, recurrent/GDN checkpoints and sidecars, exact
  prefix checkpoints, and rotating-state replacement/backfill payloads.
- SpecPrefill draft-model prompt caches and any other speculative path that
  persists prompt-derived state.
- Image-feature payloads and metadata, including per-image entries, whole-image
  batch fallback entries, partial-miss encoding and supported video features.
- Writes initiated by LRU or shared-budget eviction, RAM-cache clearing,
  pressure bypass, live-request requeue, model unload, cancellation, error
  recovery and graceful shutdown.

For `none`, also prohibit new reusable RAM retention for the selected component.
For `memory`, preserve RAM retention within that component's existing budget.
Cache-manager initialization or unrelated requests may still perform ordinary
maintenance.

Logs, usage records, stored Responses API conversation objects, chat-history
storage, media preprocessing temporary files, model files and compiler caches
are also separate from this policy. In particular, Responses
`store: false` is independent of KV persistence. OS swap is also outside the
contract. Do not describe this as preventing all disk activity for a request.

Anthropic `cache_control` continues to serve its existing compatibility and
usage-accounting role. Responses `prompt_cache_key` and
`prompt_cache_retention` do not become aliases or override this extension.
Changing their existing semantics requires a separate compatibility decision.

## Implementation approach and invariants

Use one shared request-policy model and resolver for the four endpoints. Resolve
and validate policy before any preprocessing/cache write and pass it explicitly
through both batched engine wrappers, async and sync request construction,
scheduler admission, prefix-cache operations and subordinate cache producers.
Image policy must reach the VLM preprocessing/feature paths before any lookup
promotion or store, not only scheduler admission. Keep the policy on a request
after completion while asynchronous stores retain its state. Retries, requeues
and cancellation must not reset either component to the default.

Do not toggle `hot_cache_only` or any global/model setting for individual
requests. Do not reuse `hot_cache_write_back=False`: that flag explicitly routes
state toward SSD under pressure. Do not reuse `skip_cache_store`: it suppresses
reusable storage rather than allowing RAM retention, and is used by internal
benchmarks. The new `kv: none` may reuse appropriate internal store-suppression
mechanisms, but must additionally cover intermediate snapshots, admission/requeue
stores, draft caches and every other path in this contract.

RAM entries need explicit persistence eligibility independent of `dirty` state.
Dirty means no committed disk copy exists; it does not authorize writing one.
Check eligibility before serialization into disk staging buffers or the SSD
queue, and again at persistence sinks. Ineligible entries must not wait for
writer-queue space or trigger the inline-write fallback. Cleared or evicted
ineligible entries are dropped; default-eligible dirty entries retain existing
flush behavior. Shared-budget eviction and replacement/backfill paths preserve
the permission associated with the artifact they manipulate.

Stateful models need complete, bounded RAM-resident checkpoint support alongside
existing SSD-split layouts. Do not switch a shared manager's layout or claim a
cache hit whose required recurrent state was discarded. Associate a checkpoint's
permission with its restorable block dependency. If the complete artifact cannot
fit, retain a shorter valid prefix or report a miss. Account retained checkpoint
bytes against the existing cache budget; do not create an unbounded side buffer.
The first implementation milestone must establish whether existing embedded
state can serve this without changing compatibility keys, or whether an
explicit in-memory checkpoint representation is required.

The current pressure-requeue mechanism spills a whole live batch to SSD. A
KV `memory` or `none` member must block that spill plan unless a separately
validated RAM-safe recovery is available. Keep existing safe memory-failure behavior;
do not introduce a new batch-recovery algorithm in the first version. Mixed
requests may consequently have less spill-based recovery available.

The vision-feature cache currently combines RAM insertion and disk enqueue in
`put()`, while `get()` can promote a disk hit into RAM. Separate those decisions:
`images: memory` inserts only into the bounded RAM tier; `images: none` skips
reusable insertion and lookup promotion. Cover `get_grid`, fast input assembly,
partial-hit encoding, per-image/whole-batch fallback and video paths. Continue
passing request-local computed features to the model. Never toggle the shared
engine's `_vision_cache_enabled` flag per request; mixed concurrent requests must
retain independent policies. No new global image-cache setting is needed.

Counters may report memory-only retained/dropped bytes and persistence grants
if needed to verify behavior. Do not label an estimate as measured SSD wear or
bytes saved. The response acknowledgement confirms policy, not retention.

## Acceptance and qualification

Use production paths and unique prefixes to distinguish prohibited writes from
legitimate shared/default work. A final empty cache directory is insufficient:
temporary snapshots could have been written and deleted during the request.
Instrument all covered persistence sinks and inspect temporary/staging paths.

| Case | Required result |
| --- | --- |
| Each endpoint, streaming/non-streaming, body/header | Policy reaches the engine; acknowledgement is present before output. |
| Invalid policy, conflicting inputs, unsupported engine | Error before preprocessing, model work or request-derived cache writes. |
| Independent KV/image mode combinations | Each component honors its own choice, including omitted defaults. |
| Cold KV memory-only prompt, positive RAM budget | Reusable RAM prefix where supported; no KV payload writes. |
| Repeat matching memory-only prompt | Correct prefix reuse without changing persistence permission. |
| Zero/full budget or oversized artifact | Generation can proceed where active inference fits; cache drops/misses instead of SSD fallback. |
| Default write-back and write-through | Existing default durability and reuse behavior are preserved. |
| Both components memory-only with write-through enabled | Still no attributable covered cache writes. |
| Local and shared-budget eviction, clearing, unload/shutdown | Drop ineligible entries; flush eligible dirty entries normally. |
| Prefill boundary and terminal snapshots, GDN split/embedded, rotating cache, quantized cache | Valid reconstruction and zero prohibited temporary or final payload writes. |
| SpecPrefill and supported draft/verification paths | Both target and subordinate caches honor policy. |
| `images: memory`, cold/repeated image | RAM reuse permitted; no image-feature payload or staging writes. |
| `images: none`, transient title/metadata request | Request-local features reach the model; no new reusable image-feature RAM entry or disk payload. |
| Both components `none` | No new reusable KV, checkpoint, draft or image-feature retention; active inference still completes where memory fits. |
| Image policy `none` with existing RAM/SSD entries | Existing entries remain; no lookup promotion, new insertion or persistence grant. |
| Multi-image, partial hits, whole-batch fallback and supported video | Same image policy survives all preparation and store paths without duplicate encoding solely for policy. |
| Concurrent default/memory/none image requests | No shared flag mutation or policy leakage; legitimate default grants remain possible. |
| Cancellation, retry, store worker, saturation and inline fallback | Policy survives; no hidden write or retained unbounded buffer. |
| Mixed-policy concurrent requests and eviction races | Exact artifact grants behave as specified; unmatched suffixes stay ineligible. |
| Memory-pressure batch requeue | An ineligible member cannot be silently spilled to SSD. |
| Restart or unload/reload with only memory-only artifacts | No retained artifact survives through a new disk payload. |
| Existing disk prefix reused by memory-only work | Hit permitted; no unnecessary payload rewrite. |
| Global disabled or hot-cache-only configuration | Client cannot enable caching or disk persistence beyond server policy. |

Extend existing API, hot-cache, paged SSD, prefix/GDN, snapshot, SpecPrefill,
vision-feature, VLM preprocessing, scheduler and teardown tests. Include a
controlled real-model text check and a stateful VLM check, comparing
correctness/prefix counts and recording
cache-directory payload writes, retained memory and time to first token under
equivalent conditions. Record exact revisions and configurations. Do not claim
a performance improvement or SSD-lifespan change from unit tests alone.

## Delivery sequence and unresolved choices

1. Review the two-component contract, particularly header/body naming, shared
   artifact grants and permitted disk reads. Image-feature retention control is
   included; per-image client breakpoints remain outside v1.
2. Prove bounded RAM checkpoint handling and persistence permission through the
   lower cache layers, including eviction and sink-level negative tests.
3. Wire request parsing, acknowledgement and local engine propagation together
   with prefill, draft, VLM image-feature retention, pressure recovery and lifecycle
   enforcement. Do not advertise support on any endpoint before its covered paths
   are enforced.
4. Qualify behavior against unchanged defaults, then document client examples
   and the limits of the contract. No new dashboard setting is needed for v1.
5. Integrate a qualified implementation into downstream builds and prepare a
   focused upstream contribution independent of unrelated features.

The field/header spellings and the precise RAM checkpoint representation remain
proposals. A future retention TTL, prefix pinning/priority, per-block client
breakpoints, API-key defaults, automatic client proxy and strict privacy mode
are outside v1. Existing cache budgets and default server behavior remain the
starting point.

## Related source paths

- [Request state](../../omlx/request.py) and
  [engine request construction](../../omlx/engine_core.py).
- [OpenAI request models](../../omlx/api/openai_models.py),
  [Responses request models](../../omlx/api/responses_models.py),
  [Anthropic request models](../../omlx/api/anthropic_models.py), and
  [HTTP handlers](../../omlx/server.py).
- [Scheduler snapshots, background stores and memory recovery](../../omlx/scheduler.py).
- [Prefix storage and checkpoint reconstruction](../../omlx/cache/prefix_cache.py),
  [shared RAM budget and SSD manager](../../omlx/cache/paged_ssd_cache.py), and
  [boundary snapshot writer](../../omlx/cache/boundary_snapshot_store.py).
- [SpecPrefill draft storage](../../omlx/specprefill/draft.py) and the separate
  [vision-feature cache](../../omlx/cache/vision_feature_cache.py).
