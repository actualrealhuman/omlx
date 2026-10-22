# Let the native supervisor own web-requested restarts

Feature ID: `supervisor-owned-restarts` · Availability: **Topic**

## Behavior and use

Coordinates dashboard restart requests with the native app's supervisor, so restart intent is acknowledged and the parent owns replacing the Python server.

After building the matching native/Python changes, the normal dashboard Restart action uses the supervisor path. Exercising it stops the server and requires the machine user's approval.

## Source and adoption boundary

- Source: [`5878dc7da5630c6365ef15c0219f8a9daabb1946`](https://github.com/actualrealhuman/omlx/tree/5878dc7da5630c6365ef15c0219f8a9daabb1946) (focused commit in published history).
- Baseline: [`1efcb1f91ad395179547cd48c7203dd2b03dfa96`](https://github.com/actualrealhuman/omlx/commit/1efcb1f91ad395179547cd48c7203dd2b03dfa96).
- These are fixed source snapshots, not a promise of compatibility with your target.

Carry the Python restart route/control handshake and Swift supervisor/process-runtime changes together. Port to the target's current lifecycle protocol rather than transplanting one side.

Dependencies and interactions: Requires the native supervisor architecture, not another catalog feature. The helper recipe below depends on these lifecycle semantics.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`omlx/admin/routes.py`](https://github.com/actualrealhuman/omlx/blob/5878dc7da5630c6365ef15c0219f8a9daabb1946/omlx/admin/routes.py)
- [`apps/omlx-mac/Sources/App/AppDelegate.swift`](https://github.com/actualrealhuman/omlx/blob/5878dc7da5630c6365ef15c0219f8a9daabb1946/apps/omlx-mac/Sources/App/AppDelegate.swift)
- [`apps/omlx-mac/Sources/Server/PythonRuntime.swift`](https://github.com/actualrealhuman/omlx/blob/5878dc7da5630c6365ef15c0219f8a9daabb1946/apps/omlx-mac/Sources/Server/PythonRuntime.swift)
- [`apps/omlx-mac/Sources/Server/ServerProcess.swift`](https://github.com/actualrealhuman/omlx/blob/5878dc7da5630c6365ef15c0219f8a9daabb1946/apps/omlx-mac/Sources/Server/ServerProcess.swift)

## Validation

Check acknowledgement, rejected/missing supervisor, shutdown and startup ownership, and failure reporting with isolated fixtures. An authorized live check must observe down/up and verify the replacement server identity.

Checks to inspect and adapt from the fixed source:

- [`tests/test_admin_restart.py`](https://github.com/actualrealhuman/omlx/blob/5878dc7da5630c6365ef15c0219f8a9daabb1946/tests/test_admin_restart.py)
- [`apps/omlx-mac/Tests/oMLXTests/ServerProcessIntegrationTests.swift`](https://github.com/actualrealhuman/omlx/blob/5878dc7da5630c6365ef15c0219f8a9daabb1946/apps/omlx-mac/Tests/oMLXTests/ServerProcessIntegrationTests.swift)

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

The source is a focused commit in combined public history, not a separate published topic branch. Newer upstream lifecycle code may already implement part or all of the desired outcome.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).

