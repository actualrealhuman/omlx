# Develop additions that can be adopted separately

The unit of work is a coherent addition that another person, or their coding
agent, can bring into an oMLX checkout and include in a custom build. Source
sharing does not create a binary distribution, release schedule, or support
commitment. Preserve upstream attribution and existing licenses.

## Before implementation

Give the feature a stable ID and describe its observable behavior, defaults,
non-goals, and acceptance checks. Start a dedicated branch from an explicit
upstream revision. Record required features and their order; distinguish a real
runtime prerequisite from files that merely overlap. Keep unrelated additions
and personal integration history out of that branch.

Prefer the smallest useful feature. Related functionality may form a bundle
when it shares invariants that cannot safely be split yet. Document that boundary
and the work needed to separate it. A settings switch does not by itself make
source independently adoptable.

## During implementation

Keep feature-owned policy and state in focused modules where practical. Connect
them through narrow hooks in shared scheduler, settings, API, and UI code.
Preserve the target's normal behavior when the addition is absent; define and
check disabled/default behavior when it is configurable. Do not introduce a
generic plugin framework just to satisfy this rule.

Carry implementation, configuration, migration, API/UI integration, translations,
and meaningful regression coverage together. Split shared prerequisites into
named, reviewable changes before dependent features. Keep per-feature commits
or an explicit dependency stack; do not use a combined integration branch as
the only explanation of a feature.

If a bug crosses feature boundaries, identify the affected features and place
the fix in their common prerequisite or make its application order explicit.
Do not hide a necessary follow-up only in the custom build.

## Before calling source adoptable

Create a recipe using [the feature template](features/TEMPLATE.md). It must give
an exact source commit and baseline, the implementation boundary, dependencies,
usage and activation, validation procedures, and known limitations. Name all
required follow-ups; a branch tip or last translation commit is not a recipe.

Inspect the baseline-to-feature diff for unrelated changes. Validate the
feature on the stated target with only its declared prerequisites, including
absence/disabled behavior and relevant interactions. Record the exact source
and target revisions, working-tree fingerprint when dirty, checks and results,
and untested scope. Real-model and performance claims need matching evidence.
When that isolation check has not been performed, say so explicitly.

Update the feature index and recipe when the behavior, source snapshot,
dependencies, or qualification changes. Verify public source availability before
calling a recipe publicly usable. Unpublished source remains marked unavailable;
a public documentation entry does not publish its implementation.

## Completion and shipping gate

Every change, including fixes, removals, upstream integrations and documentation
changes, includes a proportional adoption-documentation review before it is
called complete or ready to ship. Apply this gate to local custom builds as
well as source sharing. Updating the documentation is part of the work.

- Update affected recipes for changed behavior, defaults, configuration, usage,
  dependencies, required follow-ups and limitations. A fix belongs in its
  feature's recipe; it does not need a new feature ID merely to be discoverable.
- Keep catalog entries and source/baseline references consistent with the
  implementation being offered. Preserve useful historical snapshots and
  results; distinguish prepared local work from verified public source.
- Update the agent guide, integration instructions and landing-page agent
  section when discovery, adoption steps or available scope changes. Keep the
  owner's human-written opening under their control; flag needed human review
  rather than rewriting it.
- Refresh validation evidence for the exact changed source and target. Review
  relevant links and build feature records. Never carry old qualification
  forward as a result for changed code.
- Record the documentation files updated, or a specific reason no content
  change is needed, in completion evidence. Internal changes may leave usage
  unchanged; they still need this review and accurate source/test references.

Before shipping, check that the selected implementation, its recipes, catalog
and qualification agree. If required documentation remains stale, report the
implementation as awaiting documentation, not ready to ship. Keep updates with
their feature work; prepare any cross-branch catalog/navigation updates as part
of the same completion boundary.

Preparing these updates does not authorize publishing them. When code is
shipped only locally, record any pending public documentation/source update
explicitly. Before a separately authorized public shipment, reconcile the code
and documentation revisions so the landing/catalog cannot imply that missing
source or unqualified behavior is available.

## Custom builds and upstream adoption

Select code by composing the chosen source changes and their prerequisites in
an isolated target checkout, then build that checkout using its own tooling.
Feature labels in a build manifest identify code; they do not remove or select
it. The combined build is a consumer of feature work, not a prerequisite for
every adopter. Review upstream inclusion separately from source availability.

Preserve usable historical snapshots with their limitations. When upstream
provides equivalent behavior, point to that implementation and test the desired
outcome before porting an old patch. Do not claim equivalence solely from a
matching feature name or commit subject.

Local preparation never implies permission to publish, trigger hosted jobs,
install, or launch an app. Follow the applicable repository's approval and cost
rules at those boundaries.
