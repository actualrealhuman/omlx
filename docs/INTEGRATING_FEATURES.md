# Integrating an addition into your own oMLX build

Start with the [feature index](FORK_FEATURES.md), choose the behavior you want,
and use its fixed source revision and baseline to inspect the implementation.
Those revisions identify a source snapshot, not a guarantee that it fits your
checkout unchanged.

## Inspect before applying

Follow your target repository's instructions. Record its branch, commit, and
uncommitted changes, and work on a separate integration branch or worktree.
Keep an available copy of existing work and use separate test data for changes
to chat storage or caches.

Check whether your upstream version already provides the feature or changes the
same behavior. Topic branches can predate substantial scheduler and UI changes.
The index distinguishes topic-branch implementations from changes available
only inside the combined build, and calls out known dependencies.

For example, fetch the published throttle branch without merging it:

```sh
git fetch https://github.com/actualrealhuman/omlx.git \
  refs/heads/feature/inference-throttle:refs/remotes/omlx-additions/inference-throttle
git log --oneline 25aebb3ee0bb052a264241953bef6820f102dd0b..ee978a1c466323aa366f82bb831e99fe423583ea
git diff --stat 25aebb3ee0bb052a264241953bef6820f102dd0b ee978a1c466323aa366f82bb831e99fe423583ea
```

The revision in the index is fixed; a branch may acquire later commits. Inspect
the fixed revision first and evaluate subsequent fixes separately. If the
example remote-tracking ref already exists, inspect it before fetching over it.

## Apply the selected behavior

Review the full baseline-to-feature diff, including settings, API routes,
translations, UI, tests, and design notes. Cherry-picking a focused commit can
be suitable when the surrounding code matches. Otherwise, port the behavior
into your target's current interfaces and preserve newer upstream fixes.

For combined-build sources, inspect the identified files and feature commits
and extract only the relevant changes. Merging `personal/main` brings unrelated
additions with it. A feature manifest records build identity; changing feature
labels does not select code.

Resolve dependencies explicitly. Chat persistence changes share browser state
and transaction rules; battery control and throttle code share inference
boundaries; several chat UI additions edit the same template. A conflict-free
Git operation is only the start of validating these interactions.

## Validate on your target

Use the entry's suggested checks as a starting point, then inspect the tests and
[upstream testing notes](TESTING.md) at your target revision. Bring relevant
regression tests along with the implementation and adapt them to the target.
Report the exact target revision and any uncommitted changes you tested.

For web changes, check translations and regenerate CSS when needed using the
target's build tools. Exercise the affected UI with isolated data. For
inference changes, check default behavior, cancellation, streaming, concurrency,
and supported engine paths. Real-model checks and controlled measurements are
needed for performance claims. Building the native app requires its own
toolchain and checks.

Coordinate resource-intensive tests with whoever is using the machine. Follow
the target repository's approval rules for installs, launches, publishing, and
external services; integrating source does not require any of those actions.

## A prompt for your coding agent

> Integrate [feature name] from actualrealhuman/omlx into this checkout. Read its
> feature index and integration guide, and follow this repository's instructions.
> Inspect my branch, revision, and existing changes; work in an isolated branch
> or worktree. Compare the documented feature revision with its baseline and
> with my code. Explain dependencies and preserve my existing work and newer
> upstream fixes. Apply the selected behavior and relevant regression tests,
> then run appropriate local validation. Report the exact revisions, results,
> limitations, and remaining work. Ask before installing, launching, publishing,
> or using any paid or metered external service.

## Sharing a fix

Patches and pull requests are welcome. Include the selected source revision,
your target revision, reproduction steps, and validation results. Link the
feature entry so the scope is clear. Review is best effort. For changes intended
for upstream, use [upstream's contribution guidance](CONTRIBUTING.md).
