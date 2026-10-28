# Nonblocking menu bar visibility recovery

Feature ID: `nonblocking-menubar-warning`

## Behavior and use

After launch, wait three seconds and check whether the macOS status item is
visible. If it appears hidden, recreate it once and check again after one
second. If it remains hidden, write a diagnostic hint directing the user to
Settings > Appearance > Menu Bar Icon. The check runs asynchronously, so a
hidden item does not hold up app startup or prompt the user during launch.
Visible items require no recovery. The existing user-initiated Restore control
remains available for a persistent problem.

There is no user setting to enable. The watcher is started by the menu bar
controller. It tests three visibility signals (`NSStatusItem.isVisible`, the
button window's visibility, and its occlusion state); failure to obtain the
status item, button, or window is treated as hidden.

## Source and adoption boundary

- Availability: published on [`feature/nonblocking-menubar-warning` at `925cb01bd3a8b2b9d7fa06290dba4bf15210c9bb`](https://github.com/actualrealhuman/omlx/tree/925cb01bd3a8b2b9d7fa06290dba4bf15210c9bb). A 2026-10-21 availability check had found it unpublished.
- Source: implementation commit [`546ac1cf29b6ce8474c5c413e5d9214cad2f4e88`](https://github.com/actualrealhuman/omlx/tree/546ac1cf29b6ce8474c5c413e5d9214cad2f4e88) on `feature/nonblocking-menubar-warning`; the published tip adds recipe documentation.
- Source baseline: `328edc756010693e61f8bd4e47f00996e0758176`, the exact parent
  of the one-commit implementation. The patch is focused and has no unrelated
  feature commits.
- Suggested target: `bd1e9b112474862ebed550a9c83b4316038a1d74` (pinned stable
  `v0.7.1`). It is newer than the source baseline; port the feature commit onto
  it and qualify the port rather than diffing the entire source branch against
  this target.
- Required changes: apply the complete commit: `MenubarVisibilityWatcher.swift`,
  `MenubarVisibilityRecoveryTests.swift`, its source/test registration in
  `oMLX.xcodeproj/project.pbxproj`, and the testing note in `docs/TESTING.md`.
  Keep these together; the test file is compiled through the project-file
  change.
- Dependencies: uses the existing menu bar controller, status-item recreation,
  diagnostics logger, and Appearance Restore flow. These are existing app
  components rather than separately selectable feature IDs. Preserve their
  contracts when porting. The commit changes the launch-time hidden-item
  response; review any target-specific watcher or launch behavior before
  applying it.

## Validation

The source test is
`apps/omlx-mac/Tests/oMLXTests/MenubarVisibilityRecoveryTests.swift`. Run it
with:

```sh
xcodebuild -project apps/omlx-mac/oMLX.xcodeproj -scheme oMLX -destination 'platform=macOS' -only-testing:oMLXTests/MenubarVisibilityRecoveryTests test
```

The cases cover a visible icon, one recreation for a persistently hidden icon,
and an icon becoming visible after recreation. Also manually confirm on the
target macOS version that the delayed launch check does not block startup, a
visible item is left alone, and a still-hidden item gets a diagnostic hint
while the Settings Restore action remains usable. These checks are suggested;
no test or target-isolation result is claimed. The change has not been
qualified on the pinned baseline with only its declared dependencies.

## Limitations and remaining work

The recovery is best-effort: one automatic recreation cannot repair a menu bar
item that macOS continues to hide. It reports that state and leaves manual
Restore available. The three-signal probe depends on AppKit visibility state
and should be checked across the macOS versions supported by the target. Port
the focused commit to the chosen target, run the test and manual checks there,
and record the exact target revision before calling the source independently
adoptable.
