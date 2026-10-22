# oMLX additions

<!-- Human-written introduction reserved for the repository owner.
     Do not generate or paraphrase that introduction. -->

## For coding agents

This repository shares additions to [upstream oMLX](https://github.com/jundot/omlx)
as source for selected adoption into another checkout. The same source and
validation notes are available for upstream evaluation. Read this section as
navigation; the linked recipes define each implementation's actual scope.

| Requested improvement | Start here |
| --- | --- |
| Live inference limits, battery protection, model turns and relative allowances | [Inference controls bundle](docs/features/inference-controls.md) or [standalone throttle](docs/features/inference-pacing.md) |
| Understand persisted cache content | [Cache inspection](docs/features/cache-inspection.md) |
| Choose per-request KV and image retention | [Request cache policy](docs/features/request-cache-policy.md) (source publication pending) |
| Set concurrent generation limits for each model | [Per-model concurrency](docs/features/per-model-concurrency.md) (source publication pending) |
| Preserve chat history and media | [History safety](docs/features/chat-history-safety.md) and [IndexedDB storage](docs/features/chat-storage-indexeddb.md) |
| Supply your own stylesheets or select an optional True Black preset | [Custom CSS recipe](docs/features/custom-css-overrides.md) (source publication pending) |
| Improve inline editing and live context visibility | [Feature catalog](docs/FORK_FEATURES.md#feature-recipes) |
| Control benchmark uploads and improve native lifecycle handling | [Feature catalog](docs/FORK_FEATURES.md#feature-recipes) |

**[Agent starting point](docs/AGENT_START.md)** ·
[All feature recipes and source availability](docs/FORK_FEATURES.md) ·
[Integration procedure](docs/INTEGRATING_FEATURES.md)

Some additions have focused source branches; others need extraction from a
combined snapshot, and some catalog entries have no public implementation
snapshot yet. The catalog labels these differences. Cloning this documentation
branch does not enable the additions. Upstream installers install upstream oMLX.

Focused stable-source ports for chat storage, benchmark upload controls and
request cache retention are prepared locally. Their recipes identify the source
and remaining qualification; they become externally adoptable after verified
publication. Use the [composition guide](docs/FEATURE_COMPOSITION.md) when
selecting several features together.

Give your agent a request such as:

> Read the agent starting point and feature catalog in this repository.
> Integrate [feature IDs] into my current oMLX checkout, with only their required
> prerequisites. Preserve my work and newer upstream behavior, run appropriate
> local checks, and build the resulting checkout locally. Report what changed,
> how to enable it, exact revisions, validation and remaining limitations.
> Ask before installing, launching, publishing or using paid/metered services.

For new work here, follow [the feature separation policy](docs/FEATURE_DEVELOPMENT.md).
Each addition should remain a focused source change or an explicit dependency
stack with an adoption recipe.

## Upstream and license

oMLX is developed upstream at [jundot/omlx](https://github.com/jundot/omlx).
See upstream for its documentation and official installation options.
Source reuse retains the [Apache 2.0 license](LICENSE) and existing attribution.
This fork provides source with best-effort review, without prebuilt binaries,
a promised release schedule or an ongoing support commitment.
