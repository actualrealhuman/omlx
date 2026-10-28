# Custom CSS overrides

Feature ID: `custom-css-overrides` · Availability: **Published focused source**

## Behavior and use

The web interface provides one shared Style Overrides editor from dashboard
Appearance settings and Chat Settings. Users can select the bundled True Black
dark-mode preset, enable browser-local custom CSS, import or export a CSS file,
restore the previous saved state, or reset to the unchanged application theme.

The ordinary default remains App Default with custom CSS disabled. Preset
selection applies immediately; custom CSS applies after Save & Apply. Both are
restored before body rendering. Adding `?custom_css=off` to a page URL suppresses
saved styling for recovery without deleting it.

Downstream adopters that intentionally want a different initial preset may add
`<meta name="omlx-default-style-preset" content="true-black">` to the document
head. This only affects browsers without a saved preset choice; selecting App
Default remains sticky.

## Source and adoption boundary

- Availability: published on [`feature/custom-css-overrides` at `1ca6d7a5efb0de7403c71806ce81ee6e994cf0e0`](https://github.com/actualrealhuman/omlx/tree/1ca6d7a5efb0de7403c71806ce81ee6e994cf0e0).
- Source branch: `feature/custom-css-overrides`.
- Published branch tip: [`1ca6d7a5efb0de7403c71806ce81ee6e994cf0e0`](https://github.com/actualrealhuman/omlx/tree/1ca6d7a5efb0de7403c71806ce81ee6e994cf0e0); it adds recipe documentation after the implementation boundary below.
- Feature source boundary: [`abf991f4938f0c772775f312bd4340a25c2f580c`](https://github.com/actualrealhuman/omlx/tree/abf991f4938f0c772775f312bd4340a25c2f580c) (the complete branch history through that commit).
- Baseline: oMLX v0.7.1, commit
  `bd1e9b112474862ebed550a9c83b4316038a1d74`.
- Required changes: the complete branch through its recorded feature tip,
  including the base-template
  cascade hook, shared dialog and loader, editor and preset stylesheets, dashboard
  and chat entry points, locale strings, tests, and user/design documentation.
- Dependencies: none. Enhanced Readability is optional and remains independent;
  when enabled it precedes user CSS but follows the built-in preset.

The feature does not require a settings API, database migration, server-side
profile, external service, or CSS build. Static assets are served by the existing
web package.

Read the [usage guide](../custom-css.md). This entry replaces the older
[built-in dark-theme patch](high-contrast-dark-theme.md) in the current catalog.
The branch was not available through the public fork when checked on 2026-10-22
UTC; it is now publicly available at the exact tip above. Do not substitute the
older theme implementation for this feature. Public availability does not
qualify its composition with another downstream feature.

## Validation

Run these local checks from the repository root:

```sh
python3 -m pytest -q apps/omlx-web/tests/test_admin_custom_css_overrides.py
node --test apps/omlx-web/tests/custom_css_overrides.test.cjs
python3 -m pytest -q apps/omlx-web/tests/test_admin_enhanced_readability.py apps/omlx-web/tests/test_admin_a11y_changes.py
```

The focused tests cover inert defaults, early restoration, allowlisted preset
selection, final cascade order, safe-mode suppression without deletion,
`textContent` injection, size limits, shared dashboard/chat access, localization,
and True Black scoping. The readability tests check the adjacent cascade feature.

Validation on 2026-10-21 at source revision
`abf991f4938f0c772775f312bd4340a25c2f580c`:

- JavaScript runtime tests: 6 passed.
- Focused and adjacent Python tests: 21 passed, 3 deselected.
- `git diff --check`: passed.

Integration qualification on 2026-10-21 at `personal/main` revision
`1f9e69024ddf2ff25bf1d0991bbc8c94864b4ee9`:

- Full web Python suite: 448 passed, 3 deselected.
- Standalone web JavaScript suites: 184 passed.
- The former `high-contrast-dark-theme` manifest entry and its scattered base
  style overrides were removed; the affected dashboard, chat, login and focus
  styles again use the pinned v0.7.1 theme tokens.
- The downstream build configures True Black as the initial preset through the
  documented meta setting; a saved user choice still takes precedence.

Documentation review covered this recipe, `docs/custom-css.md`,
`docs/web-ui-design.md`, `PERSONAL_BUILD.md`, the build manifest, and the agent
and feature-development guidance. The source and recipe are now public at the
branch tip above; publication does not add target or composition qualification.

For a manual browser check, verify App Default in light and dark modes, select
True Black and reload dashboard/chat/login, add and disable a visible custom rule,
import/export it, restore the prior state, and confirm that `?custom_css=off`
recovers from `body { display: none !important; }` without deleting the rule.

No model or hardware prerequisite is required. Use a disposable browser profile
for the destructive recovery example so ordinary site preferences are not
disturbed.

## Limitations and remaining work

- Preferences are scoped to one browser origin and do not sync across hosts,
  ports, browsers, or devices.
- Custom CSS is trusted configuration. It may hide or imitate controls and may
  load remote assets with `url()` or `@import`.
- This is author-level CSS rather than browser-extension user-origin CSS.
  Application declarations using `!important` may require equally specific
  custom `!important` rules.
- The True Black preset is isolated to one file but may need targeted selector
  maintenance when upstream markup changes.
- Qualification after composition with other downstream features remains
  separate from public source availability.

Return to the [feature catalog](../FORK_FEATURES.md) or the
[agent starting point](../AGENT_START.md).

The published tip includes documentation-only updates after the implementation
boundary; the test revisions above remain tied to the stated source revision.
