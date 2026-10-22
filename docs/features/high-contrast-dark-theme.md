# High-contrast true-black dark theme

Historical feature ID: `high-contrast-dark-theme` · Availability: **Historical / superseded**

The current feature is [custom CSS overrides](custom-css-overrides.md), with
browser-local stylesheets and an optional True Black preset. It replaces this
patch to the application theme. The source below remains available for historical
reference; do not stack it underneath the replacement. Check the new recipe's
public source availability before selecting it.

## Behavior and use

Uses a true-black dark palette with stronger contrast in admin, Chat and login surfaces.

Select the existing dark appearance after integration; the change does not add a separate theme engine.

## Source and adoption boundary

- Source: [`183625ffa310193549ddc961533caae3f84aa07d`](https://github.com/actualrealhuman/omlx/tree/183625ffa310193549ddc961533caae3f84aa07d) on `feature/high-contrast-dark-theme`.
- Baseline: [`b390b31e0c6831225fed0f24d278eb1db7fcb68b`](https://github.com/actualrealhuman/omlx/commit/b390b31e0c6831225fed0f24d278eb1db7fcb68b).
- These are fixed source snapshots, not a promise of compatibility with your target.

Port shared dark CSS variables and the affected base, chat and login styles together. Preserve unrelated layout and functionality.

Dependencies and interactions: No other catalog addition is required. Theme rules share templates/CSS with other UI additions.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`omlx/admin/static/css/dashboard.css`](https://github.com/actualrealhuman/omlx/blob/183625ffa310193549ddc961533caae3f84aa07d/omlx/admin/static/css/dashboard.css)
- [`omlx/admin/templates/base.html`](https://github.com/actualrealhuman/omlx/blob/183625ffa310193549ddc961533caae3f84aa07d/omlx/admin/templates/base.html)
- [`omlx/admin/templates/chat.html`](https://github.com/actualrealhuman/omlx/blob/183625ffa310193549ddc961533caae3f84aa07d/omlx/admin/templates/chat.html)
- [`omlx/admin/templates/login.html`](https://github.com/actualrealhuman/omlx/blob/183625ffa310193549ddc961533caae3f84aa07d/omlx/admin/templates/login.html)

## Validation

Inspect actual dashboard, Chat, login, dialogs, inputs, focus and disabled states in dark appearance. Verify light appearance remains legible and regenerate target assets when required.

Checks to inspect and adapt from the fixed source:

- [`tests/test_admin_dark_theme.py`](https://github.com/actualrealhuman/omlx/blob/183625ffa310193549ddc961533caae3f84aa07d/tests/test_admin_dark_theme.py)

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

The historical template layout may differ from the target. A passing style assertion alone is not a visual or accessibility qualification.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).

