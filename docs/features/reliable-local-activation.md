# Local activation and restart verification helpers

Feature ID: `reliable-local-activation` · Availability: **Topic**

## Behavior and use

Provides helpers to run a staged app in place and to verify a real supervisor-owned restart against expected process/build identity.

Read each script's arguments and behavior in the fixed source before running it. Activation launches an app; restart verification controls a server. These actions require explicit authorization, even though the scripts are local.

## Source and adoption boundary

- Source: [`02dd8ddf52b904ce90ec3f428ad6e12ed33f0c07`](https://github.com/actualrealhuman/omlx/tree/02dd8ddf52b904ce90ec3f428ad6e12ed33f0c07) (focused commit in published history).
- Baseline: [`5878dc7da5630c6365ef15c0219f8a9daabb1946`](https://github.com/actualrealhuman/omlx/commit/5878dc7da5630c6365ef15c0219f8a9daabb1946).
- These are fixed source snapshots, not a promise of compatibility with your target.

The source adds the two scripts and their usage documentation. Use the adopter's own artifact paths, settings and lifecycle procedure.

Dependencies and interactions: Requires [supervisor-owned restart](supervisor-owned-restarts.md) semantics or a target-native equivalent. It is tooling, not a prerequisite for unrelated runtime additions.

Start with these production files at the source revision; inspect the full
change for related hooks, translations and configuration:

- [`apps/omlx-mac/Scripts/activate_build.py`](https://github.com/actualrealhuman/omlx/blob/02dd8ddf52b904ce90ec3f428ad6e12ed33f0c07/apps/omlx-mac/Scripts/activate_build.py)
- [`apps/omlx-mac/Scripts/verify_restart.py`](https://github.com/actualrealhuman/omlx/blob/02dd8ddf52b904ce90ec3f428ad6e12ed33f0c07/apps/omlx-mac/Scripts/verify_restart.py)

## Validation

Review argument handling, expected revision/process identity and failure paths. In an authorized isolated app session, verify a real process replacement and expected health identity; source inspection alone does not establish this.

These are adoption checks, not new test results. This documentation audit did
not run runtime suites or independently qualify this feature on a new target.
For the bundle's historical results, see the [feature index](../FORK_FEATURES.md).

## Limitations and remaining work

These scripts are not installers or a binary distribution. Their target-specific process assumptions need review. No independent live qualification was performed during this documentation audit.

Return to the [feature index](../FORK_FEATURES.md) or follow the
[agent adoption guide](../AGENT_START.md).
