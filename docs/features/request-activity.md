# Activity request observability

Feature ID: `request-activity`

## Behavior and defaults

Activity is an experimental, opt-in observer for active requests and bounded
recent request history. The admin Activity tab lists requests and provides an
inspector for submitted and prepared inputs, search, navigation, live following,
and copy/download controls. Capture is disabled by default; existing history
remains readable while capture is off. Opening the tab does not load a model.

Retention defaults are 1,000 terminal requests, seven days, and a 5 GiB physical
storage budget. The generation API families covered are `/v1/completions`,
`/v1/chat/completions`, `/v1/messages`, and `/v1/responses`. Normal
authentication remains authoritative; observer access uses the admin session
or main API key, while named inference keys identify request sources but do not
grant observer access. Credentials and authentication headers are not copied
into records.

## Source and adoption boundary

- Availability: experimental topic. The focused source snapshot is fixed, but
  verify that the exact commits are available in the source repository before
  adopting; this recipe does not establish publication or availability.
- Runtime source: [production snapshot
  `3458d61ffb97dc915e283806b7397145cedca168`](https://github.com/actualrealhuman/omlx/tree/3458d61ffb97dc915e283806b7397145cedca168).
- Recipe snapshot: [source recipe at
  `abc9ee5d7f5963c0603880a52f1b27fe4a686413`](https://github.com/actualrealhuman/omlx/blob/abc9ee5d7f5963c0603880a52f1b27fe4a686413/docs/features/request-activity.md).
- Baseline: stable `v0.7.1`, commit
  [`bd1e9b112474862ebed550a9c83b4316038a1d74`](https://github.com/actualrealhuman/omlx/commit/bd1e9b112474862ebed550a9c83b4316038a1d74).
- Boundary: apply the complete focused change from that baseline through the
  runtime snapshot. Keep the Activity package, settings and credential IDs,
  generation endpoint and lifecycle hooks, scheduler/engine hooks, admin router,
  web assets, translations, and related tests together. Do not use a personal
  integration branch as a prerequisite; no generated app or native binary is
  part of this source unit.
- Dependencies: the foundation uses the Python standard library; integration
  uses the target application's existing packages. No downstream inference
  bundle, cache policy, browser chat storage, extra database package, or hosted
  service is required. Shared-file conflicts with other features are integration
  overlap, not proof of a runtime dependency.

## Validation and qualification

The current source has 60 focused Node checks recorded at runtime commit
`3458d61ffb97dc915e283806b7397145cedca168`. Earlier backend/storage checks and
composed-build trials used other exact revisions; consult the source recipe at
that documentation snapshot for their scoped historical results. These records
do not establish independent adoption or qualify a different target.

For adoption, inspect the complete baseline-to-source diff and run the focused
Activity Python and Node suites from the source recipe. Check disabled/no-history
behavior, capture enable/disable, retention, authenticated reads, all supported
API families in streaming and non-streaming modes, and scheduler/engine
interactions. Record the exact source and target revisions, dirty-tree
fingerprint, results, and untested scope. Independent adoption and qualification
are pending. Existing browser evidence is limited to built-in Chromium and
documented synthetic/local scenarios; it is not broad browser or performance
qualification.

## Known limitations

Reported interaction issues include live-follow scrolling, copy/download and
counters during updates, representation/scope switching, and scroll-rail
movement. They have not been independently reproduced; causes are unconfirmed.
Safari and older operating systems, actual disk exhaustion, broad browser
coverage, arbitrary models/media, long contexts, concurrent production load,
sustained persistence stalls, and
universal performance remain unqualified. Composed model trials are descriptive
and do not promise faster inference. Native artifacts require a separate,
source-matched Release validation by the adopter.
