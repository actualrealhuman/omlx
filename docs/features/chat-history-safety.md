# Preserve chat history when saving fails

Feature ID: `chat-history-safety` · Availability: **Topic**

## Behavior and use

Preserves existing chat history on quota, serialization, corrupt-data or storage-access failures, and surfaces recovery choices instead of silently replacing history.

Open the built-in Chat after integration. The protection applies to normal saves; recovery UI appears on a failed load/save. Use a disposable browser origin when exercising failures.

## Source and adoption boundary

- Source: [`2e4df0898aef4502d02e745bb8e241c3a1677a84`](https://github.com/actualrealhuman/omlx/tree/2e4df0898aef4502d02e745bb8e241c3a1677a84) (focused commit in published history).
- Baseline: [`8288884d9b4f6db7b547633a94d36794c6b1d52d`](https://github.com/actualrealhuman/omlx/commit/8288884d9b4f6db7b547633a94d36794c6b1d52d).
- These are fixed source snapshots, not a promise of compatibility with your target.

The original focused change is available as a commit inside published history. Carry the storage result contract, every affected save call site, recovery UI, translations and tests. A later IndexedDB stack extends this contract and should be used as the source when adopting that stack.

Dependencies and interactions: No other catalog addition is required for the original localStorage implementation. [IndexedDB storage](chat-storage-indexeddb.md) depends on the safety invariants, not on leaving the old backend unchanged.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`omlx/admin/static/js/chat_history_storage.js`](https://github.com/actualrealhuman/omlx/blob/2e4df0898aef4502d02e745bb8e241c3a1677a84/omlx/admin/static/js/chat_history_storage.js)
- [`omlx/admin/templates/chat.html`](https://github.com/actualrealhuman/omlx/blob/2e4df0898aef4502d02e745bb8e241c3a1677a84/omlx/admin/templates/chat.html)

## Validation

Simulate quota, storage access, serialization and malformed-history failures. Check that prior bytes survive, failure is visible, callers await/check saves as appropriate, and recovery requires an explicit user choice.

Checks to inspect and adapt from the fixed source:

- [`tests/chat_history_storage.test.cjs`](https://github.com/actualrealhuman/omlx/blob/2e4df0898aef4502d02e745bb8e241c3a1677a84/tests/chat_history_storage.test.cjs)
- [`tests/test_chat_ui_overhaul.py`](https://github.com/actualrealhuman/omlx/blob/2e4df0898aef4502d02e745bb8e241c3a1677a84/tests/test_chat_ui_overhaul.py)

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

The original source predates asynchronous per-chat records. Do not overwrite a newer backend with this historical whole-history implementation. Original availability does not imply current-target compatibility.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).
