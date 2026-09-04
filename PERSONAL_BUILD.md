# Personal downstream build

`personal/main` is the public integration branch for the personal oMLX build.
It combines the current upstream release with downstream patches and feature
branches that are developed independently for possible upstream contribution.

| Change | Kind | Source branch or commit | Upstream status |
| --- | --- | --- | --- |
| Disable community benchmark uploads | Temporary downstream patch | `patch/telemetry-upload-disabled` / `e2e9e189` | Replace with an opt-in setting; uploads off by default |
| Cache inspection sidecars | Upstreamable feature | `feature/cache-inspection-sidecars` | Draft PR #3326 |
| Battery and power management | Upstreamable feature | `feature/battery-power-management` | Planned |
| Live dashboard context usage | Upstreamable feature | `feature/dashboard-context-usage` | Planned |

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
