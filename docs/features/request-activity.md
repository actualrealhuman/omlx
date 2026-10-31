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
records incomplete capture while preserving request terminal status. A disabled
recorder with no history creates neither a database nor a worker.
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
capture slots. Supported inline originals have bounded image retention and
lazy previews; effective queue limits can be lower than the attachment cap. Captured
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
`tests/test_activity_snapshot.py` checks the actual four request schemas,
explicit/null/extra fields, large-media text survival, nested references, plain
file/tool data, immutable ownership, Unicode/chunk/size/depth boundaries,
disabled behavior and clear/delete/disable/failure reservation races. It also
checks that JSON encoding executes on the writer thread.
`tests/test_activity_images.py` covers actual request schemas and PNG/JPEG/WebP
original-byte fidelity/provenance, text-first budgets, disabled retention/no
fetches, repeated references, malformed/animated/oversized containers, no codec
work, bounded authorized streaming/session expiry, clear/delete races,
transactional migration rollback and physical/count/age retention.
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
| `/requests/{id}/attachments/{attachment_id}` | Lazy, bounded-streamed original PNG/JPEG/WebP bytes |
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

## Submitted snapshot and media normalization

Submitted input is frozen once before preprocessing into a bounded immutable
semantic tree. The API admission walk does no JSON encoding, media decoding,
network fetching or model work. It preserves explicitly set fields and extras
from the validated request; unused defaults remain absent. JSON encoding and
compression run on the Activity writer, in bounded chunks even for long escaped
strings. Prepared model-input observation remains a separate representation.

Recognized inline image/audio/video/file payload fields become ordered
`_omlx_activity_attachment` references before queue admission. Small surrounding
text therefore survives media that would have overflowed a whole-JSON snapshot.
Plain text documents and ordinary tool-result JSON fields named `data` stay
text. References include source position, kind, encoded size and explicit
submitted-unvalidated provenance. Image references point to the detail attachment
index for retained/not-retained status and reasons. Other media remains explicit
not-retained references. None proves engine acceptance.
External references are bounded metadata and are never fetched by Activity.

The snapshot walk has memory/byte limits, an 8,192-node and 32-level traversal
bound, and at most 128 detailed media references. Omitted values or tails leave
explicit markers and capture gaps. Strings are shared only as immutable values
with their storage charged; request/Pydantic objects and mutable containers do
not cross to the writer. Queue space is reserved before walking and held through
clear/delete/disable or worker failure until the in-flight snapshot releases it.
This prevents deleted requests from being resurrected and avoids negative queue
accounting after a persistence failure. These are bounded structural controls,
not a completed throughput/overhead qualification.

The readable submitted view shows ordered attachment references. Submitted JSON under
More evidence exposes the retained normalized structure, including fields that
the readable message layout does not display. Copy/download reflects the chosen
view. The page explicitly labels normalized attachment payloads.

### Original image previews

When capture and `retain_images` are enabled, inline PNG, JPEG and WebP
originals are candidates after submitted text has won admission. Image decoding
from base64, bounded container/header checks and persistence run on the Activity
writer. No image codec, metadata decompression, pixel buffers, network fetches,
PIL objects, device arrays or inference preprocessing are used by the observer.
The reference is a submitted original, independently container-checked by
Activity; it is not an assertion that inference validated, accepted or used it.
Checks identify expected containers and dimensions, reject animation and cap
traversal at 4,096 chunks/segments. PNG chunk CRCs are checked; JPEG/WebP checks
are structural headers, not full codec validation. A browser can still reject
a malformed file; the inspector reports that as unavailable.

Original previews have hard 16 MiB file, 16,384-side and 16 Mi-pixel ceilings,
as well as configured per-attachment/request/queue bounds. Queue reservations
include the immutable encoded string, base64/byte/SQLite temporaries and a
conservative per-image allowance. With the default 16 MiB queue, even an
otherwise valid image can be omitted well below 16 MiB. Omission reasons remain
visible and do not discard surrounding text. Audio/video/documents, remote
fetches, processed-image reconstructions and cross-request deduplication are
outside this preview slice.

Detail indexes contain bounded metadata only, including original dimensions,
size, availability, provenance and omission reasons. Open Attachments to load
one original at a time through the admin-only byte endpoint. Request changes,
expiry, session loss and hiding Activity remove mounted images. No arbitrary
submitted URL is assigned to an image element. Streams share the eight-viewer
limit with SSE, read at most 64 KiB per short database transaction, recheck admin
authority between chunks, and stop after 60 seconds or missing/deleted data.
Responses use inert allowlisted image MIME types, no-store/nosniff and fixed
filenames; image bodies are never put in list/SSE snapshots.

Schema 2 transactionally adds request-owned attachment rows to schema 1. Reads
can still inspect schema-1 text before migration. Unsupported newer schemas
remain untouched. Originals and references cascade with clear/delete and
count/age/byte retention. Original byte lengths contribute to captured bytes
and per-request limits; SQLite page capacity and physical sidecars both enter
pre-write admission. Existing v1 binaries preserve/refuse the newer store;
rolling an app back must not downgrade or replace that history.

## Live controls

Capture settings on Activity apply without restarting inference. The configurable
1,000-record/seven-day/5-GiB defaults remain unchanged. Turning capture off closes
active records as `capture_stopped`, preserves partial content and records the
reason; it neither cancels inference nor invents a successful request completion.
Turning it back on captures only newly admitted requests. Retention reductions
apply promptly; completed history is removed first. If an active store cannot
fit a reduced byte allowance, its captures stop and become eligible for retention.

`PUT /admin/api/activity/settings` accepts `enabled`, `max_requests`,
`max_age_days`, `max_storage_bytes`, and `retain_images`; unknown fields, nulls and inconsistent
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
reports that saved/live distinction explicitly. `retain_images` controls future
submitted snapshots; changing it neither deletes existing originals nor revokes
already admitted image work. A replacement writer cannot
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

## Internal work and background scope

Only the four generation endpoints create ordinary Activity records. The
inspected admin throughput/context/accuracy/tuning entry points run outside
those observed routes; no benchmark prompts are intentionally archived by
Activity. This is a source-audited boundary, not real benchmark qualification.
Known GPU keep-warm upkeep detaches the initiating Activity context when its
long-lived task is created during lazy model loading. Other context variables
are preserved, and the foreground capture is restored even after an exception.
No GPU work or timer behavior is changed by this detachment.

`tests/test_activity_scope.py` exercises the actual task-creation method with
its GPU loop replaced by a synthetic coroutine, plus nested/error restoration.
Optional engines, future internal callers and additional background tasks still
need explicit coverage. Internal work must not inherit a user's capture merely
because it runs in a task descended from that API request.

## API errors and request outcomes

A completed HTTP200 stream is not necessarily successful inference. The four
API formatters and JSON/SSE keepalive handlers signal a terminal semantic
failure separately when they actually surface one. Activity retains an
`api_failure_outcome` and `api_failure_source: api_formatter` in protected
metadata, even if the content queue cannot retain the error body. It combines
that signal with existing engine and transport outcomes; later HTTP completion
cannot erase it, and finalization still waits for observed engine invocations.
No response-body parsing, error-message copying or additional engine cancellation
occurs on the inference path.

On these instrumented paths, guard rejections remain rejected, request/mid-prefill
aborts are signaled cancelled, and formatter/terminal tool failures are errors.
Existing outcome precedence still reconciles independently observed engine and
transport failures. A recovered tool-marker quotation in prose does not signal
failure. Signals are request-scoped, idempotent and fenced by capture disable,
clear/delete and terminal finalization. Ordinary HTTP exceptions and unhandled
iterator errors keep their existing status/transport observation paths.

`tests/test_activity_failures.py` first reproduced all four formatter errors
being recorded as completed under HTTP200. It now checks the actual formatters
inside the observer/ASGI transport, completed/no observed invocation variants,
actual JSON grace and JSON/SSE keepalive classification, outstanding engine work,
content-capture refusal, disabled/closed scopes, terminal tool failure versus
recovered prose and concurrent success/failure isolation. These synthetic checks
qualify that integration boundary, not every backend's abort or full endpoint
behavior. Real-model and native lifecycle qualification remain pending.

## Browser inspector

The Activity tab opens independently of capture being enabled. The list reads
metadata; selecting a request loads its bounded retained ranges in a dedicated
browser worker. Submitted input shares a readable view with structured API output. Model input
shows observed prepared text followed by producer output. When supported API wire
is unavailable, the submitted view retains a separately labeled producer fallback.
Input token IDs, producer text, original API wire and invocation events remain
distinct evidence choices.
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

## Readable API output

The inspector worker projects captured JSON/SSE from the four generation API
families into separate answer, API-exposed reasoning, refusal, error and tool-call
sections. It consumes the formatter output that was actually retained, without
re-running a model's thinking/tool parser or changing inference capture. Raw
producer text and API wire evidence remain available unchanged. Models/backends
that do not expose reasoning or tools in the API do not acquire invented ones.
The Structured API output evidence choice shows this projection without input.

Choice, Anthropic block and Responses output-item indexes remain separate.
Fragmented tool arguments append to their own call; initial empty Anthropic tool
input is replaced by the argument deltas. Responses item-done and terminal full
snapshots replace earlier deltas, avoiding duplicate reasoning/answers/calls and
preserving authoritative final text. Responses sequence duplicates/reordering
and gaps are flagged. Unknown blocks/events, malformed envelopes, missing starts,
interruption and hard limits are explicitly partial. A captured terminal envelope
means protocol termination was observed; it does not mean inference succeeded or
that capture had no gaps. API errors appear in their own inert text section.

Parsing runs only in the selected inspector's dedicated browser worker. It is
incremental across fetched ranges and UTF-8 decoder boundaries. It supports JSON
keepalive whitespace, SSE comments, CRLF and multiline data fields. Per-projection
limits are 1 Mi-character SSE events/lines, 16 Mi-character JSON bodies and output
text, 4,096 sections, and JSON structural limits of 32,768 punctuation/nodes and
64 levels before native parsing. Tool names/IDs/namespaces are bounded at 256
characters; truncation is explicitly partial. These are additional to the aggregate 64 MiB
selection fetch limit. Hitting them stops/coarsens this readable projection; it
does not discard the retained wire or throttle inference. Search/copy/download
operate on the selected view, visibly partial for incomplete projections, capture
gaps or loading/size limits. The API wire choice supports inspection of fields
that the readable projection does not map, including signatures and usage.

`tests/test_activity_output.py` feeds the actual four production streaming
formatters' synthetic-engine output, actual response builders and formatter error
paths through the browser parser at multiple fragment sizes. These checks cover
formatting, not full authenticated endpoint execution or real-model behavior.
`activity_output.test.cjs` covers fragmented/multiple calls and choices, block
ordering, final replacement, errors/partial/unknown data, sequences and resource
limits. The actual worker harness additionally checks a long Unicode response,
far-tail search, complete selected exports, input/output ordering and preservation
of model/raw/JSON evidence modes. Rendered-browser behavior remains unqualified.

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
- The bounded semantic walk runs once before preprocessing. Qualify its overhead
  with concurrent requests, large structures and actual model serving; the current
  checks prove structural caps and handoff behavior, not serving performance.
- Actual image-preview interaction, browser pixel-decoder memory, repeated
  large-image throughput and native restart/long-running viewer lifecycle remain
  to be qualified. The first source slice now includes supported inline previews.
- Actual rendered-browser validation, text selection across virtualized windows,
  per-message copying, complete large-outline
  navigation and deep-link history polish remain to be qualified or completed.
  Native browser Find sees only mounted text; use the inspector search instead.

No source publication, packaged binary, compatibility guarantee or upstream
submission is implied by this in-progress recipe.
