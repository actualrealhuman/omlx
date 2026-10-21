# Style overrides and custom CSS

The web interface can apply an optional built-in style preset and browser-local
custom CSS without changing the application's base stylesheets. Open **Settings
→ Appearance → Style overrides** in the dashboard, or **Chat Settings → Style
overrides** in chat.

## Built-in presets

- **App Default** uses the application's original light and dark themes.
- **True Black** keeps light mode unchanged and changes dark mode to pure black
  surfaces, white primary text and higher-contrast adjacent colors.

Selecting a preset applies it immediately and stores the selection in the
current browser. A preset is independent of the Light, Dark and Auto theme
choice; True Black becomes visible only while the resolved theme is dark.

## Custom CSS

Enable Custom CSS, paste CSS rules into the editor, and choose **Save & Apply**.
The browser applies those rules after the application styles, the selected
preset and Enhanced Readability. Rules with equal specificity therefore win by
cascade order. Existing application declarations marked `!important` may still
require an `!important` custom declaration.

CSS is limited to 65,536 bytes. It can be imported from or exported to a `.css`
file. **Use True Black as a starting point** loads the bundled preset source into
the editor and selects the matching preset so it can be adjusted with later
custom declarations. The editor also retains the previous saved state; **Restore
previous** swaps it with the current state.

These settings use browser `localStorage`. They do not change server settings,
sync to other browsers or devices, or survive clearing site data. A different
host name or port has separate browser storage.

Packagers may set `data-default-style-preset="true-black"` on the document root
to make True Black the initial choice for new browser profiles. The upstream
default remains App Default, and any user selection takes precedence thereafter.

## Recovery and trust boundary

Custom CSS is trusted configuration. It cannot directly execute JavaScript, but
it can hide or imitate controls and can request remote assets with `url()` or
`@import`. Review CSS from other people before saving it.

If a rule makes the interface unusable, add `?custom_css=off` to any oMLX web
page URL. For example:

```text
http://<host>:<port>/admin/?custom_css=off
```

Style safe mode suppresses both the preset and custom CSS for that page load
without deleting saved content. Open the style editor there, correct the CSS or
choose **Reset**, and then remove `custom_css=off` from the address.
