# Personal downstream build

`personal/main` is the public integration branch for the personal oMLX build.
It combines the current upstream release with downstream patches and feature
branches that are developed independently for possible upstream contribution.

| Change | Kind | Source branch or commit | Upstream status |
| --- | --- | --- | --- |
| Benchmark upload privacy controls | Downstream feature | `patch/telemetry-upload-disabled` / `e2e9e189` plus integration follow-up | Global and per-kind settings; master permission off by default |
| Cache inspection sidecars | Upstreamable feature | `feature/cache-inspection-sidecars` | Draft PR #3326 |
| Battery and power management | Upstreamable feature | `feature/battery-power-management` | Planned |
| Live dashboard context usage | Upstreamable feature | `feature/dashboard-context-usage` / `260c66fd` | Implemented and tested; PR not opened |
| Autosizing chat message editor | Upstreamable feature | `feature/chat-edit-autosize` / `20dba1ca` | Implemented and tested; PR not opened |
| Explicit-only message edit cancellation | Upstreamable fix | `feature/chat-edit-safe-cancel` / `c26317ff` (`4294bd5e` integration) | Implemented and tested; PR not opened |
| Preserve interrupted chat output | Upstreamable fix | `feature/chat-preserve-interrupted-output` / `86fd1d63` | Implemented and tested; PR not opened |

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
