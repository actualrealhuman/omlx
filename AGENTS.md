# Working with these oMLX additions

This branch is a source-sharing entry point. Reading it does not install any
addition. Start with [the agent adoption guide](docs/AGENT_START.md), select IDs
from [the feature index](docs/FORK_FEATURES.md), then read only their recipes and
linked implementation sources. Follow the target checkout's instructions when
adapting code there; this guide does not override them.

For development in this fork, follow [the feature separation
policy](docs/FEATURE_DEVELOPMENT.md). Keep each feature in a focused branch or
explicit dependency stack with implementation, relevant tests and a recipe.
Keep unrelated personal integration history out of feature branches. Record
combined-source coupling honestly until it has been removed and qualified.

Before declaring any change complete or ready to ship, apply the policy's
completion and shipping gate: update affected recipes/catalog, source and
validation references, and relevant agent/landing navigation. Record the
documentation review even when no content change is needed. Preserve the
owner's human-written introduction. Pending public updates remain explicit
and require separate publication approval.

Before editing, inspect the branch, exact revision and existing changes. Preserve
others' work. Validate only the changed behavior and required repository checks;
do not treat old results as qualification of a changed target. Coordinate heavy
tests with the machine's user. Keep local state and private handoffs outside the
public checkout; retain licenses and attribution.

Do not use paid or metered services, trigger hosted jobs, or create configuration
that will trigger them without an immediate warning and explicit authorization.
Any external write, including pushes, default-branch changes and pull requests,
also requires explicit approval for that exact action immediately beforehand.
Installing or launching the app requires separate explicit authorization.
Source-sharing work does not authorize a binary distribution or release service.
