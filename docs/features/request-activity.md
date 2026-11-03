# Activity request observability

Feature ID: `request-activity`

## Current status

**Status: Experimental.** Activity is an opt-in observer for active requests
and bounded recent request history. The focused source snapshot is complete at
`3458d61ffb97dc915e283806b7397145cedca168`, based on stable `v0.7.1`
(`bd1e9b112474862ebed550a9c83b4316038a1d74`). The discovery branch is
[`feature/request-activity`](https://github.com/actualrealhuman/omlx/tree/feature/request-activity).
Verify that the exact source revision is available there before adoption;
this recipe alone does not establish public availability. The current implementation
has been exercised in a composed local build and the built-in Chromium browser.
It has not been independently adopted and qualified from this recipe.

| Evidence | Revision and result | Scope |
| --- | --- | --- |
| Backend and storage checks | `7fe132c12c3890bf1d744f70a0073e29db05d103`: 1,828 Python checks and 18 subtests passed; one existing native-extension skip | Earlier focused backend snapshot; not a result for the later UI/source snapshot or an independent adoption. |
| Current focused web checks | `3458d61ffb97dc915e283806b7397145cedca168`: 60 Node checks passed | Deterministic web behavior checks on the current feature source; not full app or cross-browser qualification. |
| Built-in Chromium | Current implementation exercised in synthetic and local application flows | Browser behavior and bounded inspector/resource probes; user-trial issues below remain reported, not diagnosed. |
| Composed model/observer trials | `40f860e1dd8e8e0b3a9bdccf3cb44ce7c81d6abf`: extended local text, image, persistence-failure, overhead, and concurrent-capture exercises | Composed build and specific local workloads; not independent adoption or universal performance evidence. The strict C1 cross-run output-hash equality check failed: it stopped after six requests, and separate capture-off/off controls also differed; cause is unknown. The separate C2 per-request fidelity check passed: each retained response matched its own client stream. It does not establish cross-run output identity. |

The evidence above is intentionally scoped. Backend checks and model trials
refer to their stated, different revisions; none are claims against an
independent adoption or a universal workload. For the dated historical record
of detailed validation, see the [original recipe at focused commit
`6efc59a882daddae60af3dd7dac105ea2f00015c`](https://github.com/actualrealhuman/omlx/blob/6efc59a882daddae60af3dd7dac105ea2f00015c/docs/features/request-activity.md).
That record preserves intermediate results and superseded pending statements;
it is historical evidence, not current acceptance status. Native artifacts
require a separate source-matched Release validation by the adopter.

## Behavior and defaults

The four generation API families—`/v1/completions`, `/v1/chat/completions`,
`/v1/messages`, and `/v1/responses`—can record accepted semantic input and API
response bytes. Scheduler and engine hooks additionally record prepared input,
token IDs, producer output, and request/invocation lifecycle. The admin Activity
tab provides request lists and an inspector with submitted/prepared views,
search, navigation, live following, and copy/download controls. Inline images
are retained within limits and previewed lazily.

Capture is disabled by default. Administrators can enable capture from Activity
settings; existing history remains readable while capture is off. Opening the
tab does not load a model. The admin session or main API key grants observer
access; named inference keys identify request sources but do not grant observer
access. Normal authentication remains authoritative, and credentials or
authentication headers are not copied into Activity records.

Retention defaults are 1,000 terminal requests, seven days, and a 5 GiB physical
storage budget. These are configurable; 1,000 is not a hard ceiling. Internal
safety limits include 64 MiB per request, 16 MiB per attachment, 128 MiB
observer memory, a 16 MiB content queue, and 1,024 active capture slots.
Effective queue limits may be lower than attachment limits. One background
SQLite writer handles persistence; a disabled recorder with no history creates
neither a database nor a writer. Existing history can still be maintained while
capture is off.

Request IDs are created before model loading/preflight, with separate IDs for
engine invocations. Streaming completion waits for the response body and
observed invocation terminal states; interrupted requests retain partial
output. Requested model and serving/filter model attribution remain separate.
Unknown token counts remain unavailable rather than being displayed as zero.
Elapsed spans describe observed receipt time, not decoding latency.

## Adoption recipe

Apply the complete focused change from the stated baseline through the exact
production snapshot in an isolated target checkout. Preserve the Activity
package, shared hooks, settings, API and UI wiring, translations, and tests as a
unit. Later commits build on the capture, storage, and integration foundation;
they are not independent features. Resolve shared-file conflicts against the
chosen target deliberately. Do not use a personal integration branch as a
prerequisite. No generated app or native binary is part of this source feature.

Implementation boundary includes `omlx/activity/`; Activity settings and
credential IDs in `omlx/settings.py`; generation endpoint, startup, and
authentication hooks in `omlx/server.py`; request, scheduler, engine-pool,
engine-core, and batched-engine hooks; admin router inclusion; Activity web
assets/templates and dashboard links; and the related server and web tests.

The foundation uses the Python standard library. Server and UI integration use
the target application's existing packages. No extra database package,
downstream scheduling controls, browser chat storage, or hosted service is
required. Capture remains off until enabled.

For a local source review, verify the pins and inspect the complete baseline
diff before adoption:

```sh
git diff --check bd1e9b112474862ebed550a9c83b4316038a1d74..3458d61ffb97dc915e283806b7397145cedca168
git diff --stat bd1e9b112474862ebed550a9c83b4316038a1d74..3458d61ffb97dc915e283806b7397145cedca168
git diff bd1e9b112474862ebed550a9c83b4316038a1d74..3458d61ffb97dc915e283806b7397145cedca168
```

Run focused Activity tests from the repository root, then validate the
disabled/no-history behavior, capture enable/disable, retention, authenticated
read access, each supported API family in streaming and non-streaming modes,
and relevant scheduler/engine interactions. These are adopter validation
steps, not results claimed by this recipe. Test the exact source and target
revisions used, record any dirty-tree fingerprint, and report untested scope.
Independently verify public source availability before treating this recipe as
publicly usable.

```sh
python -m pytest tests/test_activity.py tests/test_activity_routes.py \
  tests/test_activity_api_paths.py tests/test_activity_runtime.py \
  tests/test_activity_engine.py tests/test_activity_writer.py \
  tests/test_activity_snapshot.py
node --test apps/omlx-web/tests/activity_document.test.cjs \
  apps/omlx-web/tests/activity_ui.test.cjs \
  apps/omlx-web/tests/activity_output.test.cjs
```

## Known issues and limitations

Reported interaction issues have not been independently reproduced in a focused
investigation; causes are unconfirmed:

- Following output may not remain pinned after manually scrolling to the
  bottom.
- Copy, download, and the message/block counter may flicker during updates.
- Switching representation or scope may show unavailable content until the
  request is selected again.
- The scroll rail may move in visible steps and may not consistently reach its
  endpoints.

Qualification is limited to the built-in Chromium browser and documented
synthetic/local scenarios. Safari and older operating systems, actual disk
exhaustion, broad browser coverage, arbitrary models/media, long contexts,
concurrent production load, sustained persistence stalls, universal
performance, and independently composed builds remain outside the established
scope. Short composed-build model and performance trials are descriptive only;
they do not imply that Activity accelerates inference or passes a general
performance target.
