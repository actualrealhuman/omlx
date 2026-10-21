# Custom CSS overrides

Feature ID: `custom-css-overrides`

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
`data-default-style-preset="true-black"` to the document root. This only affects
browsers without a saved preset choice; selecting App Default remains sticky.

## Source and adoption boundary

- Availability: focused topic branch; public branch publication is pending.
- Source branch: `feature/custom-css-overrides`.
- Feature source boundary: `4b30b06e5361e71176bdebe1e31178eab4cfd1bf`
  (the complete branch history through that commit).
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
`4b30b06e5361e71176bdebe1e31178eab4cfd1bf`:

- JavaScript runtime tests: 6 passed.
- Focused and adjacent Python tests: 21 passed, 3 deselected.
- `git diff --check`: passed.

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
- Public branch publication and qualification after composition with other
  downstream features remain separate actions.
