# Activity request observability

Feature ID: `request-activity`

## Behavior and use

Activity is being implemented as an opt-in observer for current requests and
bounded recent context history. The current source contains the capture/storage
foundation, settings persistence, request lifecycle and initial engine hooks.
The four generation API families capture accepted semantic input and their API
response stream when explicitly enabled. Local scheduler/core hooks additionally
capture prepared input/tokens and producer output. Admin read endpoints and
snapshot-based live delivery feed a top-level Activity browser tab. The initial
inspector has submitted/prepared views, an outline, a semantic rail, literal
search, follow-output and copy/download. This is not a complete feature or an
independently qualified adoption snapshot; rendered-browser and real-model
qualification remain pending.

The foundation uses one background SQLite writer and separately bounded content
and lifecycle metadata. Producer methods enqueue immutable CPU observations;
encoding, compression and persistence happen on the worker. Queue saturation
records incomplete capture while preserving request terminal status. A disabled recorder with no history creates neither a database nor a worker.
Existing history uses a maintenance worker so retention and deletion still apply
when capture is off.

Request identity begins before model loading/preflight. Engine invocations have
separate identifiers under the request. Streaming completion waits for the body
and observed invocation terminal states, with partial output retained on
interruption. Named credentials use opaque IDs and historical label snapshots;
legacy IDs are persisted when capture is enabled without writing unrelated
runtime overrides back into saved settings. Normal authentication remains the
authority; credentials and HTTP authentication headers are not copied from
authentication into Activity records.

Defaults: disabled capture, 1,000 terminal requests, seven days, and a 5 GiB
physical storage budget. Counts and budgets are configurable; 1,000 is not a
hard ceiling. Internal safety defaults include a 64 MiB request cap, a 16 MiB
attachment cap, 128 MiB observer memory, a 16 MiB content queue and 1,024 active
capture slots. Attachments are not implemented by this foundation. Captured
content is distinct from aggregate serving statistics and reusable KV caches.

## Source and adoption boundary

- Availability: local focused implementation in progress; no public source
  snapshot is offered for adoption yet.
- Discovery branch: `feature/request-activity`.
- Baseline: stable `v0.7.1`, full commit
  `bd1e9b112474862ebed550a9c83b4316038a1d74`.
- Current boundary: `omlx/activity/`, Activity settings and credential IDs in
  `omlx/settings.py`, generation endpoint/startup/auth hooks in `omlx/server.py`,
  input/output/lifecycle hooks in `omlx/engine_core.py`, `omlx/scheduler.py`,
  `omlx/request.py` and `omlx/engine/batched.py`, the admin router inclusion,
  the Activity web assets/templates and dashboard links, plus server Activity
  tests and focused web tests.
- Foundation dependencies: Python standard library only. Server and UI wiring
  will use the application's existing packages. No dependency on downstream
  scheduling controls or browser chat storage is intended; full isolation
  qualification remains pending.

Before a complete source snapshot is offered, this recipe must include the
immutable feature revision, actual endpoint/backend matrix, activation UI/API,
full implementation boundary, validation and any remaining prerequisites.

## Validation

Run CPU-only foundation checks from the repository root:

```sh
python -m unittest discover -s tests -p test_activity.py -v
```

They exercise actual recorder/storage behavior, including disabled resources,
settings round-trip, source/model filtering, pagination, compressed Unicode
ranges, large text/token snapshots, concurrent producers, saturated/stalled
writers, request limits, lifecycle capacity, clear/delete fences, terminal
retention, partial shutdown/restart and corrupt/newer store preservation.
These checks do not qualify inference integration, browser UI, real models or
performance. Run relevant subsystem tests after their hooks are implemented.
`tests/test_activity_runtime.py` exercises endpoint validation/auth ordering,
semantic snapshots, stream finalization, disabled behavior and credential
migration. `tests/test_activity_engine.py` exercises actual scheduler/core hook
paths with mock models, including prepared-token reuse, producer output,
deferred cancellation and failed insertion. Those need the normal test runtime;
shared test imports may initialize Metal even though no real model is loaded.
`tests/test_activity_metrics.py` checks multiple invocations, cumulative-counter
idempotence/regression, unavailable counts, capture-off fences, scalar evidence
through queue loss and safe admission-option filtering. The engine integration
check invokes the actual prefill callback without re-tokenizing prepared input.
`tests/test_activity_routes.py` covers actual admin authorization, bounded reads,
pagination/filtering, snapshot consistency, reconnect/session revalidation,
reader cancellation and stream admission. `tests/test_activity_transport.py`
covers handled exceptions and responses whose iterators never begin.
`tests/test_activity_controls.py` covers live disable/resume without engine
cancellation, atomic section-only persistence, stale versions, invalid settings,
save failures, disabled-history maintenance, clear/delete fences, shared-lock
ownership after caller cancellation, slow-writer replacement guards, and physical-
budget reduction/reclamation.
`tests/test_activity_dashboard.py` checks parent identity correlation for actual
waiting/prefill/generation row construction. Web tests compile the Alpine
expressions and render the real dashboard template. Run the standalone behavior
checks with:

```sh
node --test apps/omlx-web/tests/activity_document.test.cjs apps/omlx-web/tests/activity_ui.test.cjs
```

These exercise large single messages, long lines, sparse indexing, Unicode/range
boundaries, complete literal search of loaded retained text, bounded match/DOM
metadata, stable character anchors, reader suspension, stale-response exclusion,
URL navigation and browser-height limits. Synthetic checks do not substitute for
actual light/dark/mobile browser review.

## Admin read interface

The existing `/admin/api/activity` dashboard summary is preserved. New endpoints
under that prefix require the existing admin session or main API key; named
inference keys do not grant access:

| Endpoint | Data |
| --- | --- |
| `/status` | Capture health and Activity settings, without local paths |
| `/requests` | Metadata-only keyset pages; active/recent, model, source, state and time filters |
| `/facets` | Bounded model and historical credential-label choices |
| `/requests/{id}` | Metadata, retained representation indexes and a version fingerprint |
| `/requests/{id}/content` | Bounded raw byte ranges for a representation/invocation |
| `/events` | Committed metadata snapshots with optional selected-request indexes |

Content is never embedded in list or live snapshot responses. Content ranges
use UTF-8 byte offsets and `application/octet-stream`; the response supplies
offset/next-offset/total-byte headers. Consumers reading sequential ranges must
use an incremental decoder rather than decode incomplete character fragments
independently. Reads use separate short-lived SQLite snapshots and do not create
a missing database. Existing history can be inspected with capture disabled.

Live delivery polls committed metadata at 500 ms, with a maximum of eight
viewers. A dedicated two-thread reader pool admits at most four outstanding
jobs, including work for disconnected callers. No browser queue or reader is
on the inference producer path. List and selected-content indexes in each live
snapshot come from one committed read transaction.

The first event is a `snapshot` with an epoch/sequence ID. Reconnect uses
`resync_required` followed by a fresh snapshot; replay is explicitly
`snapshot_only`. Clients fetch new text ranges from retained content. Session
authorization is rechecked throughout a stream; `authorization_lost`,
`unavailable` and `reconnect` are explicit terminal stream events. Data responses
are marked no-store. This is not a lossless event-log/replay contract.

## Live controls

Capture settings on Activity apply without restarting inference. The configurable
1,000-record/seven-day/5-GiB defaults remain unchanged. Turning capture off closes
active records as `capture_stopped`, preserves partial content and records the
reason; it neither cancels inference nor invents a successful request completion.
Turning it back on captures only newly admitted requests. Retention reductions
apply promptly; completed history is removed first. If an active store cannot
fit a reduced byte allowance, its captures stop and become eligible for retention.

`PUT /admin/api/activity/settings` accepts `enabled`, `max_requests`,
`max_age_days`, and `max_storage_bytes`; unknown fields, nulls and inconsistent
limits are rejected. Send `X-Activity-Action: settings` and optionally `If-Match`
with the `config_version` from status to reject stale saves. Only the Activity
section is saved, preserving stored values versus unrelated runtime overrides.
Credential-ID migration uses the same serialized settings-save boundary.

`DELETE /admin/api/activity/requests/{id}` requires JSON `{"confirm":"delete"}`
and `X-Activity-Action: delete`. `POST /admin/api/activity/clear` requires JSON
`{"confirm":"clear"}` and `X-Activity-Action: clear`. The browser asks for local
confirmation with the exact scope. These remove Activity data and fence late
writes; they do not clear chat history/KV caches or cancel inference. Clearing
also stops capture of requests already admitted, while later requests can still
be captured. No forensic erasure is promised.

Mutations require the existing admin authority, an explicit action header and
bounded control admission. A task retains the shared settings-save lock through
its worker result even if the HTTP caller disconnects. On a failed or timed-out
response, reload settings/history before deciding whether to retry; a save can
have succeeded even if its later live application did not finish. The API
reports that saved/live distinction explicitly. A replacement writer cannot
start while the previous writer is still stopping.

## Observed execution evidence

Metadata includes scalar counts for observed scheduler invocations, admitted
input token IDs and increases in each invocation's cumulative generated-token
counter. Repeated output snapshots do not add tokens twice. A regressing
counter records a capture gap; no count is derived from character or byte length.
Absent counters stay unknown, including in terminal invocation events. Counts
can remain available when bounded payload capture drops token/text evidence;
retained-representation flags distinguish that case.

The existing scheduler prefill callback supplies the Prefilling label where
observed. First observed token/text times use monotonic elapsed time since
Activity receipt, persisted as durations. They describe producer updates and
are not HTTP first-byte latency or a measurement of GPU execution time.

Invocation events include a bounded subset of scalar sampling values at core
admission, prepared-input counts, terminal outcome and the observed finish reason.
This is not a complete effective-options snapshot. Detail responses explicitly
report the evidence sources and retained representations. Engine-producer text
is still not universally pre-parser text; unobserved backends and later sparse/
requeue/attention transformations remain qualification work.

## Browser inspector

The Activity tab opens independently of capture being enabled. The list reads
metadata; selecting a request loads its bounded retained ranges in a dedicated
browser worker. Submitted messages and engine output share one view. Model
input shows observed prepared text followed by engine output; input token IDs,
API wire response and invocation events remain distinct evidence choices.
Unavailable representations and capture gaps are labeled.

The worker keeps full retained text for literal, case-sensitive search and
copy/download, with a 64 MiB per-selection preview ceiling. Search and exports
are explicitly partial during loading or at that ceiling. No prompt bodies are
written to browser localStorage. At most 20 request reading positions persist
in memory during the page session; subscriptions, worker reads and timers stop
when Activity or the browser page is hidden.

Sparse line checkpoints subdivide huge messages, newline-free lines and runs
of combining characters. Only a bounded viewport is mounted, with at most 200
text rows per worker response. A bounded physical scroll axis avoids browser
maximum element-height limits for very tall documents. The semantic rail,
outline and search target logical message/character anchors. Live appends retain
that anchor unless Follow output is selected. Outline/rail metadata is capped
at 2,000 sections; search rail marks at 500, with full match counts retained.

Observed dashboard waiting/prefill/generation rows link to the parent Activity
request. Links support modified clicks and browser history. Uncorrelated or
synthetic aggregate rows remain unlinked. Interface strings use locale keys;
new non-English entries currently use the repository's English fallback.

## Limitations and remaining work

- Complete per-invocation query summaries, full effective options, richer timing/
  cache/paused-state evidence and all backend/endpoint qualification. Current
  generation hooks are covered by mock-model checks, not real-model validation.
- `raw_output` currently means engine-producer text before API formatting.
  Engines with their own native parsers may already have transformed it;
  universal pre-parser raw generation is not yet qualified.
- Qualify the initial inspector, navigation, dashboard links and live controls
  in an actual browser. Assess image support without delaying the core text MVP
  unnecessarily; current media placeholders are inert.
- Qualify physical-budget behavior under disk-full conditions, pinned SQLite
  readers, reclamation/migration and very large histories. Current page limits
  conservatively reserve part of the budget for WAL/sidecars; configured disk
  bytes are not a promise of equal logical content capacity.
- Qualify hot-path overhead, worker memory, faithful raw/parsed output,
  cancellations, all required API modes and optional-engine boundaries.
- Validate a fully composed private build separately from the focused source.
- Semantic input export currently runs once at API ingress before preprocessing;
  its cost on very large payloads and media normalization still need qualification.
- Attachments and native restart/long-running viewer lifecycle remain to be
  completed or qualified. Image previews are not implemented yet.
- Actual rendered-browser validation, text selection across virtualized windows,
  per-message copying, richer parsed reasoning/tool output, complete large-outline
  navigation and deep-link history polish remain to be qualified or completed.
  Native browser Find sees only mounted text; use the inspector search instead.

No source publication, packaged binary, compatibility guarantee or upstream
submission is implied by this in-progress recipe.
