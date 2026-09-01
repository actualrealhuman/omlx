# Private downstream patch ledger

This file and the patches marked **private** are maintained only in the
private omlx downstream. Upstreamable work is developed on clean `feature/*`
branches based on `upstream/main` and submitted through the public fork.

| Change | Kind | Local branch or commit | Upstream status |
| --- | --- | --- | --- |
| Disable community benchmark uploads and telemetry block | Private | `e2e9e189` | Not intended for submission |
| Cache inspection sidecars | Upstreamable | `feature/cache-inspection-sidecars` | Draft PR #3326 |
| Battery power management | Upstreamable | `feature/battery-power-management` | Planned |

## Integration policy

- `main` remains an exact fast-forward of `upstream/main`.
- `private/main` is the tested personal build and receives upstream updates by
  merge.
- Private changes are pushed only to the `private` remote.
- Upstream pull-request branches are pushed only to the public `origin` remote.
