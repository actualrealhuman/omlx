# Adopt selected oMLX additions with a coding agent

Use this guide when asked to bring additions into an existing oMLX checkout and
build it locally. The repository supplies source and implementation knowledge;
the adopter owns the resulting checkout and build. There is no fork binary
distribution to download or release service to depend on.

## Read only what the selection needs

1. Read the target checkout's instructions and inspect its branch, full revision
   and working-tree changes. Preserve existing work and use an isolated branch
   or worktree for integration.
2. Choose feature IDs from [the catalog](FORK_FEATURES.md#feature-recipes).
   Read their recipes, then only the relevant fixed source, design notes and
   tests. The documentation branch itself does not enable any feature.
3. Check whether the target already supplies the desired behavior. Compare
   interfaces and outcomes, not just names. Avoid reintroducing an old
   implementation when current upstream already satisfies the request.
4. Resolve the source boundary and prerequisites using the table below. Explain
   the smallest coherent selection, including necessary follow-up fixes.
5. Follow [the integration procedure](INTEGRATING_FEATURES.md), validate on the
   target, and build that resulting source tree using its own build tooling.

## Resolve the adoption unit

Use the [composition guide](FEATURE_COMPOSITION.md) to distinguish prerequisites,
alternative implementations and shared-file overlap for common selections.

| Recipe availability | How to proceed |
| --- | --- |
| Topic | Inspect its fixed baseline-to-source diff. Apply or port the complete focused behavior and relevant tests. A focused commit may live in combined public history without a topic branch. |
| Prepared topic (local) | A complete local source boundary is prepared. Read the recipe's qualification state; external adoption must wait until the exact source is publicly available. |
| Extraction | Use the identified modules and hooks in the combined snapshot. Extract selected behavior; do not merge the full personal build. |
| Bundle component | Inspect shared prerequisites before extracting. The whole inference bundle is available, but do not silently add it when only one component was requested. Explain the scope choice if extraction needs substantial new work. |
| Historical variant | The public source differs from the later desired boundary. Extract and qualify the requested subset or use the documented newer alternative. |
| Unpublished | No usable public implementation snapshot is offered. Report that boundary; do not invent a branch, guess local revisions or treat the recipe as implementation. |
| Historical / upstream overlap | Check the target's existing behavior first and port only a missing outcome. |

Separate required dependencies from optional companions and shared-file overlap.
For example, autosizing and explicit edit cancellation share textarea markup but
neither requires the other. IndexedDB storage requires the history-safety
invariants. Standalone throttle and the newer inference bundle are alternative
ports of overlapping behavior, not two features to stack mechanically.

The old patched dark theme is also an alternative historical implementation,
not a prerequisite for custom CSS overrides. The stylesheet feature restores
the application theme and supplies True Black as an optional preset. Check its
recipe for publication status before attempting adoption.

## Preserve the feature's contract

Carry settings/API/UI, translations, migrations, defaults and meaningful tests
alongside runtime code. Old snapshots may use `omlx/admin/` for web assets where
the target now uses `apps/omlx-web/omlx_web/`; map the behavior to current paths.
Never replace a whole current template just to obtain a small editor change.

Configure synthetic settings, cache and browser data for validation. Preserve
the target's newer cancellation, finalization, cache and model-support fixes.
An absence of Git conflicts does not establish correct integration. A saved
feature ID does not select source code or prove it is running.

## Validate and build locally

Run the recipe's relevant checks and the target repository's required checks.
Coordinate resource-heavy tests with the machine user. Record source and target
revisions, uncommitted changes/fingerprint, actual results and untested scope.
Keep old source results clearly dated; they are not results for this new port.

Use the target revision's supported build commands; historical recipe baselines
span different packaging layouts. Preserve native kernel/toolchain requirements
and verify the staged artifact's source identity and required assets. Stage the
artifact locally without replacing or launching the installed app. A local
custom build does not imply publishing a distribution.

Follow the target's approval rules. Obtain explicit authorization immediately
before app installation/launch, external writes or metered services, including
configuration that triggers hosted jobs. This guide authorizes none of those.

## Report a result another person can use

Report selected IDs and prerequisites; exact donor baseline/source and final
target revisions; what was applied or already present; how to enable/use the
features; checks and limitations; and the local artifact path if built. Explain
any remaining coupling or unavailable source instead of declaring completion
from a feature label or historical test result.

For new work in this fork, use [the development policy](FEATURE_DEVELOPMENT.md)
and [recipe template](features/TEMPLATE.md) so the next adopter can repeat the
process without reconstructing its design from the combined build.
