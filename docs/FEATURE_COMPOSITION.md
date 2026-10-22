# Choose source units for a custom build

Choose the behavior first, then its smallest complete source unit. An adoption
unit includes the implementation and its required integration hooks, migration,
translations and regression coverage. It can contain several related controls
when their correctness depends on shared state. Check each recipe for exact
source, baseline, public availability and qualification before applying it.

## Selection rules

| Requested behavior | Source to select | Dependencies and alternatives |
| --- | --- | --- |
| Safer saves with the original chat backend | [History safety](features/chat-history-safety.md) | A focused historical implementation. Keep the target's current backend and stream finalization if they already satisfy its invariants. |
| Per-chat records, IndexedDB and media retention | [Complete storage stack](features/chat-storage-indexeddb.md) | Includes history-safety invariants and asynchronous migration/recovery. Do not apply the old whole-history backend on top of this stack. |
| Choose automatic benchmark submissions | [Upload controls](features/benchmark-upload-controls.md) | No inference, cache, storage or packaging feature is a prerequisite. Local results remain usable with uploads denied. |
| Limit concurrent requests for one model | [Per-model concurrency](features/per-model-concurrency.md) | Distinct from model-turn priority. Neither should be inferred from the other. |
| Choose new reusable cache retention per request | [Cache policy](features/request-cache-policy.md) | Independent KV and image components; requires the supported target cache interfaces. It does not change conversation storage. |
| Inspect content in persisted cache blocks | [Cache inspection](features/cache-inspection.md) | Optional alongside request policy. When combined, restrictive requests must not authorize inspection persistence/backfill. |
| A single global generation work-share control | [Standalone throttle](features/inference-pacing.md) | Does not require battery policy or model turns. An alternative to importing the larger inference bundle. |
| Relative model/class limits, model turns, battery protection and their shared panel | [Inference controls bundle](features/inference-controls.md) | A coherent published integration unit. Individual component recipes identify source seams; they do not certify separately extracted implementations. Do not overlay the standalone throttle implementation. |
| Supply stylesheets or select True Black | [Custom CSS overrides](features/custom-css-overrides.md) | No old dark-theme patch is required. Defaults and preferences belong to this feature's own recipe. |
| Verify a staged native app's restart | [Activation helpers](features/reliable-local-activation.md) | Requires supervisor-owned restart semantics or the target's equivalent. Running the helpers controls an app and needs separate approval. |

Other focused UI/native additions are listed in the [catalog](FORK_FEATURES.md).
Sharing a template or a settings file is an integration overlap, not necessarily
a runtime dependency. Preserve only the requested changes when resolving it.

## Resolve multiple selections

1. Read every selected recipe against its own baseline. Exact source revisions
   can be different even when both features were developed for oMLX.
2. Include each shared prerequisite once. Keep alternative implementations out
   of the same target: old and new chat backends, old and new theme approaches,
   or two competing throttle implementations must not be stacked mechanically.
3. Port shared hooks to the target's current interfaces. Preserve unrelated
   upstream fixes and existing user work.
4. Bring regression tests along and check the combination, not just each source
   diff independently. Important interactions include cache inspection with
   retention policy, storage with stream finalization, and per-model concurrency
   with model-turn ownership.
5. Build the selected checkout. A manifest's feature labels describe inclusion;
   editing those labels does not compose or remove code.

The catalog separates source availability from target qualification. A focused
local branch may be ready for review while still unavailable to an external
adopter. An older public source can be inspectable without being qualified on a
new target. Keep those distinctions in the result reported to the user.

If a requested subfeature still shares unseparated runtime interfaces, use its
component map to plan a bounded port and qualify it. Do not silently import
unrequested personal-build features to make the patch apply.
