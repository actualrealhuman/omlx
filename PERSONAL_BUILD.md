# Personal downstream build

`personal/main` is the public integration branch for the personal oMLX build.
It combines the current upstream release with downstream patches and feature
branches that are developed independently for possible upstream contribution.

| Change | Build feature ID | Kind | Source branch or commit | Upstream status |
| --- | --- | --- | --- | --- |
| Benchmark upload privacy controls | `benchmark-upload-controls` | Downstream feature | `patch/telemetry-upload-disabled` / `e2e9e189` plus integration follow-up | Global and per-kind settings; master permission off by default |
| Cache inspection sidecars | `cache-inspection` | Upstreamable feature | `feature/cache-inspection-sidecars` | Draft PR #3326 |
| Battery and power management | `battery-power` | Upstreamable feature | `feature/battery-power-management` | Planned |
| Live dashboard context usage | `live-context` | Upstreamable feature | `feature/dashboard-context-usage` / `260c66fd` | Implemented and tested; PR not opened |
| Autosizing chat message editor | `chat-edit-autosize` | Upstreamable feature | `feature/chat-edit-autosize` / `20dba1ca` | Implemented and tested; PR not opened |
| Explicit-only message edit cancellation | `safe-edit-cancel` | Upstreamable fix | `feature/chat-edit-safe-cancel` / `c26317ff` (`4294bd5e` integration) | Implemented and tested; PR not opened |
| Preserve interrupted chat output | `stream-recovery` | Upstreamable fix | `feature/chat-preserve-interrupted-output` / `86fd1d63` | Implemented and tested; PR not opened |
| Supervisor-owned web restarts | `supervisor-owned-restarts` | Downstream fix | `5878dc7d` | Implemented and tested; PR not opened |
| Reliable local app activation checks | `reliable-local-activation` | Downstream fix | `02dd8ddf` | Implemented and tested; PR not opened |

## Build identity

Packaged personal builds retain the canonical upstream semantic version for
update comparisons and add separate identity metadata:

- channel (`private` by default);
- Git source revision and branch;
- feature IDs for every downstream feature group in the package;
- `custom-kernels` when the optional native-kernel build flag is used.

The native About and Status screens, the server health/status JSON, and the
CLI startup banner expose this metadata. Override the channel or base feature
list for an intentional variant with `OMLX_BUILD_CHANNEL` and
`OMLX_BUILD_FEATURES` when invoking `apps/omlx-mac/Scripts/build.sh`.

`omlx/_build_manifest.json` is the machine-readable source of truth for the
canonical release branch, upstream comparison ref, channel, and feature IDs.
Every downstream feature addition or removal must update both that manifest
and the table above in the same integration commit.

## Branch policy

- `main` remains an exact fast-forward of `upstream/main`.
- Each upstreamable change is developed on a clean `feature/*` branch based on
  `upstream/main` and submitted independently through the public fork.
- `personal/main` is the tested combined build. Feature branches merge into it;
  it never merges back into a feature branch.
- Personal release builds are produced from `personal/main`, not from an
  individual feature worktree.
- The public `origin` remote may carry `personal/main` as well as the clean
  feature branches. Upstream pull requests use only their matching feature
  branch.
- If an upstream squash merge or feature rebase makes the integration history
  awkward, `personal/main` may be reconstructed from current `upstream/main`
  plus the still-needed branches listed above.

## Private release procedure

1. Fetch `upstream` immediately before integration, then inspect the incoming
   commits. Do not describe a checkout as current merely because its existing
   remote-tracking ref looks current.
2. Merge the freshly fetched `upstream/main` into `personal/main`. Preserve the
   upstream `omlx/_version.py` value when resolving conflicts.
3. Integrate each downstream feature into `personal/main`, update the manifest
   and feature table, and verify that the working tree is clean.
4. Run `apps/omlx-mac/Scripts/build.sh release --preflight-only`. The preflight
   refuses a release from another branch, a dirty tree, a branch that does not
   contain the locally tracked upstream ref, or an upstream-version mismatch.
5. Run the relevant tests and then the full local release build. Release
   artifacts default to a version/build/revision-specific directory under
   `apps/omlx-mac/build/Artifacts/`, so a different build cannot overwrite the
   previous staged bundle.
6. Dry-run `apps/omlx-mac/Scripts/install_build.py --dry-run`, which verifies
   the signature, version, build number, channel, revision, branch, feature
   array, and downgrade policy. Pass `--app /path/to/oMLX.app` to select an
   artifact instead of using the newest canonical one.
7. Install only with an explicit `apps/omlx-mac/Scripts/install_build.py --yes`.
   It gracefully stops the old server, atomically exchanges the app bundles,
   launches and verifies the new server identity, retains the previous bundle
   under `~/Library/Application Support/oMLX/app-backups/`, and automatically
   rolls back if verification fails.

A dated integration branch is disposable review space, never a private release
source. For an intentional local-only Release build from another branch, the
explicit escape hatch is `OMLX_ALLOW_NONCANONICAL_RELEASE=1`; artifacts built
that way are not canonical private releases.
