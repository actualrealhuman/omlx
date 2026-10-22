# Autosizing inline message editor

Feature ID: `chat-edit-autosize` · Availability: **Topic**

## Behavior and use

Makes the inline message editing textarea grow to fit its content on opening, typing and window resize.

Edit a long or multiline Chat message. The editor resizes automatically; there is no additional setting.

## Source and adoption boundary

- Source: [`20dba1ca9c2a128eae63439bc80268f7c40e1507`](https://github.com/actualrealhuman/omlx/tree/20dba1ca9c2a128eae63439bc80268f7c40e1507) on `feature/chat-edit-autosize`.
- Baseline: [`e467261edc786efd33b1e9023d5c4a827f8aa1c1`](https://github.com/actualrealhuman/omlx/commit/e467261edc786efd33b1e9023d5c4a827f8aa1c1).
- These are fixed source snapshots, not a promise of compatibility with your target.

The topic changes inline editor CSS, initialization, input/resize handlers and its helper in `chat.html`, plus regression assertions.

Dependencies and interactions: No storage backend change is required. It shares editor markup with [explicit cancellation](safe-edit-cancel.md); combine the two handlers deliberately.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`omlx/admin/templates/chat.html`](https://github.com/actualrealhuman/omlx/blob/20dba1ca9c2a128eae63439bc80268f7c40e1507/omlx/admin/templates/chat.html)

## Validation

Check initial height, additional lines, deletion/shrinking, window resize, keyboard save, composition input, and focus/cursor position in the actual browser.

Checks to inspect and adapt from the fixed source:

- [`tests/test_chat_ui_overhaul.py`](https://github.com/actualrealhuman/omlx/blob/20dba1ca9c2a128eae63439bc80268f7c40e1507/tests/test_chat_ui_overhaul.py)

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

Adapt the behavior to the current editor lifecycle rather than replacing the full historical template. Existing static assertions do not establish visual behavior.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).
