# Share personal additions as source

- Status: Accepted
- Decision date: 2026-10-10

## Purpose

This fork makes useful additions from a personal oMLX build available for
others to integrate into their own checkouts, manually or with a coding agent.
It welcomes focused fixes and contributions with best-effort review.

## Scope

Provide a feature index with identifiable public source, source baselines,
known dependencies, and validation pointers, plus a reusable integration guide.
Describe older topic branches and combined-build sources honestly. Publication
of an index is not evidence that every addition works on every upstream version.

Combined inference controls may be documented as a prepared candidate before
publication, with a fixed commit and explicit validation status. Update those
references if source changes after qualification. Keep the already-published
Inference Throttle entry distinct: the combined work does not replace that
source.

Prebuilt binaries, a release schedule, and general compatibility or support
commitments are outside this scope. The personal build's upstream integration
and local packaging policy is a separate concern.

## Consequences

Readers choose their additions and own their resulting build and validation.
The documentation branch can serve as the fork's entry point while `main`
remains an upstream mirror and feature branches retain their own histories.
Keep the guide useful by updating source references when publishing a feature
or a relevant fix, without creating an automatic publication or build service.
Document scheduling percentages as relative allowances and activity, not power
measurements. State coverage and starvation tradeoffs without implying a
universal performance result or ongoing maintenance commitment.

See the [feature index](../FORK_FEATURES.md) and
[integration guide](../INTEGRATING_FEATURES.md).

## 2026-10-21 refinement

The [separable source decision](0003-separable-feature-source.md) and
[development policy](../FEATURE_DEVELOPMENT.md) make feature boundaries and
agent adoption recipes part of new work. Existing combined sources remain
explicitly labeled until independent extraction is qualified. The landing page
reserves its opening for the owner's human-written introduction and links a
separate [agent starting point](../AGENT_START.md).
