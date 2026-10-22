# <Feature title>

Feature ID: `<stable-id>`

## Behavior and use

Describe the user-visible improvement, normal defaults, activation/configuration,
and one concrete usage example. State what remains unchanged or outside scope
only where needed to understand the feature's contract.

## Source and adoption boundary

- Availability: focused topic / extraction from combined source / bundle
  component / unpublished / historical equivalent now upstream.
- Source: public repository URL, branch for discovery, and full immutable commit.
- Baseline: full commit; distinguish a release tag from a development snapshot.
- Required changes: complete feature commits or bounded files, production hooks,
  settings, API/UI, translations, migrations and tests, including follow-ups.
- Dependencies: required feature IDs and order; alternatives; optional companion
  features; shared-file overlap that is not a runtime dependency.

For combined sources, explain what to extract and what must stay out. For
unpublished code, say that no public source is available and omit unusable fetch
instructions. Do not infer independence from a feature flag.

## Validation

Name exact source test paths and concrete behavior checks, including ordinary
defaults, absence/disabled behavior, failure/cancellation and relevant feature
interactions. Identify any local model/hardware prerequisites and how to keep
user data and running workloads safe. Historical results need exact revisions
and dates; suggested checks are not claimed results. Record whether integration
with only the declared prerequisites has actually been qualified.

## Limitations and remaining work

State known coupling, migration/data constraints, upstream overlap and missing
qualification. Include the next action needed for independent adoption. Record
performance or energy results only within the measured workload and hardware.
