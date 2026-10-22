# Require explicit cancellation of message edits

Feature ID: `safe-edit-cancel` · Availability: **Topic**

## Behavior and use

Removes Escape-key cancellation of an inline message edit, avoiding accidental loss of draft edits. The Cancel button remains the explicit cancellation action.

While editing a Chat message, Escape keeps the edit open; use Cancel to discard it or the normal save action to commit it.

## Source and adoption boundary

- Source: [`c26317ff05ed779c0056aefc81bb80ae3fb97821`](https://github.com/actualrealhuman/omlx/tree/c26317ff05ed779c0056aefc81bb80ae3fb97821) on `feature/chat-edit-safe-cancel`.
- Baseline: [`b908f563c32378f6479a093093fb988b76e05b7c`](https://github.com/actualrealhuman/omlx/commit/b908f563c32378f6479a093093fb988b76e05b7c).
- These are fixed source snapshots, not a promise of compatibility with your target.

This focused topic removes the editor's Escape handler and updates regression coverage. Preserve the target's other editor handlers.

Dependencies and interactions: No other addition is required. Shares textarea markup with autosizing, without depending on it.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`omlx/admin/templates/chat.html`](https://github.com/actualrealhuman/omlx/blob/c26317ff05ed779c0056aefc81bb80ae3fb97821/omlx/admin/templates/chat.html)

## Validation

Verify Escape leaves draft text intact, explicit Cancel works, save works, and keyboard/composition/focus behavior remains usable.

Checks to inspect and adapt from the fixed source:

- [`tests/test_chat_ui_overhaul.py`](https://github.com/actualrealhuman/omlx/blob/c26317ff05ed779c0056aefc81bb80ae3fb97821/tests/test_chat_ui_overhaul.py)

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

This recipe describes the source's precise keyboard change, not a general unsaved-changes guard across navigation or browser closure.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).

