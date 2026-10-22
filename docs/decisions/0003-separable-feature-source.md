# Keep additions separable for source adoption

- Status: Accepted
- Decision date: 2026-10-21

## Context

An addition should be discoverable and adoptable by another person's coding
agent without reconstructing the entire personal integration. The same source
boundary should let upstream evaluate the change. Providing compiled packages
and an ongoing distribution service is outside this source-sharing purpose.

## Decision

Develop each addition as a coherent source unit with a stable ID, focused branch
or explicit dependency stack, and a recipe. Preserve implementation, settings,
UI/API, migrations and relevant tests together. Use narrow integration hooks
where practical; avoid unrelated feature coupling. The [development
policy](../FEATURE_DEVELOPMENT.md) defines the working requirements and links
the recipe template.

Feature IDs describe included code, not a composition mechanism. State existing
combined-source dependencies honestly until separate adoption is qualified.
Keep unrelated additions out of a feature branch and shared prerequisites
explicit. A custom build consumes selected features; it must not become the
only source from which their behavior can be understood.

## Consequences

Feature completion includes its adoption boundary, usage, tests, source and
baseline revisions, dependencies and limitations. Public availability and
current-target qualification are separate claims. Historical implementations
remain available with their scope; upstream equivalents should be preferred
when they meet the requested outcome.

Each completion and shipping decision includes a proportional review/update of
the affected recipes, catalog, source references, qualification and relevant
agent/landing guidance. Record that review even if no content change is needed.
Required stale documentation prevents a ready-to-ship claim; pending public
updates remain explicit and separately authorized. The owner's human-written
opening remains under their control.

Existing features need an adoption audit and may require future extraction.
This decision does not assert that all current features are already independent,
require a generic plugin system, or authorize publication, hosted automation,
app installation or launch.
