# Benchmark upload privacy controls

Feature ID: `benchmark-upload-controls`

## Behavior and use

Automatic community benchmark submission is disabled by default. The persisted
master setting `benchmark_uploads.enabled` must be enabled before any automatic
benchmark upload can occur. Separate `throughput_enabled` and
`accuracy_enabled` switches let the user permit either kind while retaining the
other switch's value when the master setting is off. Missing, malformed, or
uninitialized settings fail closed.

The admin web UI exposes the controls under Settings > Auth & Info > Benchmark
Privacy. The macOS app exposes them under Settings > Security > Benchmark
Privacy. Changes are saved through the existing global-settings API. The
uploader rechecks the setting at each network-request boundary, so switching
off stops later throughput submissions and stops the separate accuracy raw
upload after its summary request. A request already in flight cannot be
revoked; an accuracy summary already accepted remains published even when its
question-level raw upload is skipped. The UI reports skipped throughput rows
and an accepted accuracy summary whose raw data stayed local. If enabled, the
existing automatic upload paths may submit eligible local throughput results
and eligible accuracy summaries with associated question-level results to
`omlx.ai`. Existing nonlocal-endpoint exclusions and accuracy eligibility
limits still apply.

## Source and adoption boundary

- Availability: local focused adoption branch; public availability of these
  implementation commits has not been verified.
- Source: `feature/benchmark-controls-adoption`, adoption commits
  `5b6f5616ed5b98d1d50818708d8cf503a7aec82e`,
  `854fd6cfc6ba359b51cf142643841e4c19244a73`, followed by
  `a6d8fc6e42f594aeb0aa9c0f1ef3410ff54ce641` and test correction
  `c9241b218cb929252836306cb9aaa7e9bacb5442` (latest source revision).
- Baseline: `bd1e9b112474862ebed550a9c83b4316038a1d74` (the pinned stable
  `v0.7.1` release). This is a focused port of the controls and upload gates
  present in canonical `personal/main` at
  `8560a3d0080aa078e7494354b9426b375c40f125`, originally introduced with
  build identity work in `f372c31a658a2d315280b81201e5c83088194f16`. The
  adoption commits include only the permission-control source boundary and
  tests; build identity, profile packaging, and local installer changes are
  excluded.
- Required changes: take all four adoption commits in order. Together they add
  the persisted
  settings model and fail-closed policy helper in `omlx/settings.py`; guards
  automatic throughput and accuracy submissions in `omlx/admin/benchmark.py`
  and `omlx/admin/accuracy_upload.py`; carries the settings through
  `omlx/admin/routes.py`; and adds web dashboard controls and skipped-result
  messaging under `apps/omlx-web/omlx_web/`. The macOS settings API and Security
  screen changes are in `apps/omlx-mac/Sources/Net/DTO/GlobalSettingsDTO.swift`,
  `apps/omlx-mac/Sources/AppView/ViewModels/SecurityScreenVM.swift`, and
  `apps/omlx-mac/Sources/AppView/Screens/`. The test boundary is
  `tests/test_settings.py`, `tests/test_admin_server_aliases.py`,
  `tests/test_accuracy_upload.py`, `tests/test_benchmark.py`, and
  `tests/test_benchmark_upload_settings_ui.py`. The follow-up rechecks policy
  before each HTTP request and reports partial throughput skips and accuracy
  summary/raw split outcomes.
- Dependencies: existing throughput and accuracy benchmark/upload paths,
  global settings persistence, the authenticated admin global-settings API,
  and the existing web and macOS settings clients. No separate feature ID or
  build-identity/package profile is required. Keep the backend policy guard
  together with both client surfaces and the skipped-result messaging; a UI
  toggle alone does not control submissions.

## Validation

Focused local test command:

```sh
python -m pytest -q tests/test_settings.py tests/test_admin_server_aliases.py tests/test_accuracy_upload.py tests/test_benchmark.py tests/test_benchmark_upload_settings_ui.py
```

These tests cover fail-closed defaults and malformed saved values, persistence,
API read/write behavior, mocked throughput and accuracy upload paths with the
disabled policy, and a policy toggle during the first throughput POST or after
the accuracy summary POST. They passed on the candidate checkout at source
revision `c9241b218cb929252836306cb9aaa7e9bacb5442`: 629 passed in 3.42s. The
transport tests use mocks and do not contact `omlx.ai`. No packages were
installed. The macOS Settings screen and native DTO path have no feature-specific
unit test in this source; compile and manually verify the three controls,
persistence, error recovery, and disabled status messages on the target before
declaring qualification.

For a target check, start from settings with no `benchmark_uploads` section and
confirm submissions stay disabled. Exercise all master/per-kind combinations
through each settings client, restart and confirm the values persist, verify a
throughput run reports a disabled skip without changing its local results, and
verify an accuracy suite reports a disabled skip. Only enable submission for a
separate explicitly approved check that may send benchmark data externally.
These results cover focused Python units only; native UI behavior, full
server-and-client integration, and real upload behavior remain unqualified.

## Limitations and remaining work

The web copy and native default strings are currently English-only; add and
review translations before claiming localized settings coverage. The native
settings UI lacks feature-specific unit coverage, and native controls,
persistence, error recovery, and disabled status messages have not been manually
verified. The focused Python suites passed on this pinned-stable-derived
candidate, but no native or end-to-end upload qualification is claimed. Record
those remaining checks at the exact target revision before calling the feature
independently adoptable.

Prepared branch: `feature/benchmark-controls-adoption`, tip `0fe9fed12e2f9e5d3b4edde636167a2e73721b17`
(implementation plus recipe updates).

Return to the [catalog](../FORK_FEATURES.md) or
[composition guide](../FEATURE_COMPOSITION.md).

The prepared tip includes a documentation-only update dating the source-owned
publication-review note. The implementation and recorded test revisions above
are unchanged; verify the remote before treating this source as public.
