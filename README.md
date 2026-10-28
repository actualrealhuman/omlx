# actualrealhuman's oMLX additions

<!-- Human-written introduction reserved for the repository owner.
     Do not generate or paraphrase that introduction. -->
Hi! I'm **actualrealhuman** and this is my additions-fork of [oMLX](https://github.com/jundot/omlx)!

Here are the features I've added:


**Inference Throttle** (Gas Pedal)\
Reduce energy consumption, heat, fan noise, and prevent battery drain! Specify limits on a per-model or per-type basis: set your immediate/interactive models to 100% for the fastest responses; set non-interactive models to something lower (like 50%) to put agentic work and background jobs on the back burner. You can use *any percentages you like* and changes happen in **real time!!** Work using too much heat? Drag the slider and watch the heat, noise, and power use go down!


**Battery Preservation and Reserve**\
Slows down and even halts inference if your battery level drops! If you're running jobs overnight on your MacBook, this can save you from waking up in the morning to a battery with only 8% remaining!

**One Model At A Time**\
Decoding two or more models at once means that using more than one model often crashes throughput--now it doesn't have to! Requests to different models can take turns, so that you're maximizing throughput per-model! Instead of two or more models fighting for memory bandwidth, give your whole memory bandwidth to one model at a time and watch them fly!

**Per-Model Max Streams**\
Some models, like Qwen3.8-27B can decode up to eight streams efficiently, but others like Qwen3.8-Flash-Next are better off with two or one. Before you could only choose a maximum for the entire oMLX server; now each model gets its own limits!


**Custom CSS**\
So you can use your own stylesheets with ease (and no browser extensions)!


**Better Chat Saving**\
Use IndexedDB to save more than 100 chats, instead of silently deleting your oldest chats! File attachments survive reloading too, so no image placeholders!


**Per-Request Cache Control**\
(Requires client support) **Save your SSD** by telling oMLX not to cache a prompt! This can save you 100s of terabytes of SSD write wear!!


**Plain-Text Cache Sidecars**\
Provides text files for cached KV files so you know what's filling up your SSD!

**And more!**\
Point your agent at this repo and have it tell you all about it!





## For coding agents

This repository shares additions to [upstream oMLX](https://github.com/jundot/omlx)
as source for selected adoption into another checkout. The same source and
validation notes are available for upstream evaluation. Read this section as
navigation; the linked recipes define each implementation's actual scope.

| Requested improvement | Start here |
| --- | --- |
| Live inference limits, battery protection, model turns and relative allowances | [Inference controls bundle](docs/features/inference-controls.md) ([adoption snapshot `d861a30`](https://github.com/actualrealhuman/omlx/tree/d861a301e2630db33e41cfb95087e09adae6f43e)) or [standalone throttle](docs/features/inference-pacing.md) |
| Understand persisted cache content | [Cache inspection](docs/features/cache-inspection.md) |
| Choose per-request KV and image retention | [Request cache policy](docs/features/request-cache-policy.md) ([public source `455d1fc`](https://github.com/actualrealhuman/omlx/tree/455d1fcdec98e775f5be4007770dd963e3e0a084)) |
| Set concurrent generation limits for each model | [Per-model concurrency](docs/features/per-model-concurrency.md) ([public source `40742bf`](https://github.com/actualrealhuman/omlx/tree/40742bf7462a8b08c19e87da7bd4b73d62a4e58b)) |
| Preserve chat history and media | [History safety](docs/features/chat-history-safety.md) and [IndexedDB storage](docs/features/chat-storage-indexeddb.md) ([public source `f9ec698`](https://github.com/actualrealhuman/omlx/tree/f9ec698a11e6d6086be28141134ee2a97898d66b)) |
| Supply your own stylesheets or select an optional True Black preset | [Custom CSS recipe](docs/features/custom-css-overrides.md) ([public source `1ca6d7a`](https://github.com/actualrealhuman/omlx/tree/1ca6d7a5efb0de7403c71806ce81ee6e994cf0e0)) |
| Improve inline editing and live context visibility | [Feature catalog](docs/FORK_FEATURES.md#feature-recipes) |
| Control benchmark uploads and improve native lifecycle handling | [Feature catalog](docs/FORK_FEATURES.md#feature-recipes) ([benchmark source `0fe9fed`](https://github.com/actualrealhuman/omlx/tree/0fe9fed12e2f9e5d3b4edde636167a2e73721b17); [menu-bar source `925cb01`](https://github.com/actualrealhuman/omlx/tree/925cb01bd3a8b2b9d7fa06290dba4bf15210c9bb)) |

**[Agent starting point](docs/AGENT_START.md)** ·
[All feature recipes and source availability](docs/FORK_FEATURES.md) ·
[Integration procedure](docs/INTEGRATING_FEATURES.md)

Some additions have focused source branches; others are available only inside a
combined snapshot or historical variant and need extraction for a different
boundary. The catalog labels these differences. Cloning this documentation
branch does not enable the additions. Upstream installers install upstream oMLX.

Public stable-source ports for chat storage, benchmark upload controls and
request cache retention have recorded focused tests. See [historical validation
and remaining checks](docs/FORK_FEATURES.md#recorded-local-validation) and each
recipe before selecting them. Public availability does not qualify a port on a
different target. Use the [composition guide](docs/FEATURE_COMPOSITION.md) when
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
