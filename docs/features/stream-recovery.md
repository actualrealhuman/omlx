# Interrupted output preservation: historical source

Feature ID: `stream-recovery` · Availability: **Historical / upstream overlap**

## Behavior and use

The historical change retains received assistant output when streaming stops or fails. Current targets may already provide the desired behavior.

First check your target by stopping/failing a bounded stream and reloading its chat. Upstream v0.7.1 already preserves partial content in stream failure handling; compare its [Chat implementation](https://github.com/jundot/omlx/blob/bd1e9b112474862ebed550a9c83b4316038a1d74/apps/omlx-web/omlx_web/templates/chat.html).

## Source and adoption boundary

- Source: [`86fd1d6386849535f83834458b13c708ff0849a7`](https://github.com/actualrealhuman/omlx/tree/86fd1d6386849535f83834458b13c708ff0849a7) on `feature/chat-preserve-interrupted-output`.
- Baseline: [`e467261edc786efd33b1e9023d5c4a827f8aa1c1`](https://github.com/actualrealhuman/omlx/commit/e467261edc786efd33b1e9023d5c4a827f8aa1c1).
- These are fixed source snapshots, not a promise of compatibility with your target.

Retain this old source for reference, not as a routine patch to newer oMLX. If the target lacks the outcome, adapt only the missing finalization behavior and translations, preserving its storage and stale-stream guards.

Dependencies and interactions: Interacts with chat-history safety and IndexedDB storage, but preserving partial output is a different concern from safe persistence.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`omlx/admin/templates/chat.html`](https://github.com/actualrealhuman/omlx/blob/86fd1d6386849535f83834458b13c708ff0849a7/omlx/admin/templates/chat.html)

## Validation

Check Stop and network failure with visible content/reasoning/tool-round output, reload, explicit error reporting, deletion during a stream and variant changes. Keep modern finalization paths intact.

Checks to inspect and adapt from the fixed source:

- [`tests/test_chat_stream_cancellation.py`](https://github.com/actualrealhuman/omlx/blob/86fd1d6386849535f83834458b13c708ff0849a7/tests/test_chat_stream_cancellation.py)

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

The standalone downstream stream-recovery feature is retired for stable v0.7.1 integration. The old interruption badge/metadata is not a requirement. Do not infer all storage safety behavior is upstream merely because partial-output handling is.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).

