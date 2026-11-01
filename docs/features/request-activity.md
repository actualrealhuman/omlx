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
independently qualified adoption snapshot; initial synthetic browser spot checks
have passed, while broader browser and real-model qualification remain pending.

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

New records retain the bounded requested model separately from the current
serving/filter model. A resolution marker is set only when the serving-model
hook supplies an ID, including same-name resolution. Unresolved/default choices
and older rows without this evidence remain distinguishable; historical aliases
are not reconstructed. Current and paged list cards expose request ID, endpoint,
model attribution, observed input/generated token counts, elapsed receipt span,
source and capture status without reading context bodies. Unknown counts remain
unavailable rather than zero; older rows keep their known model name.

List rows and the selected inspector share one visible-only receipt clock,
which stops on hiding, authorization loss or when no loaded unfinished records
remain. Live ticks use monotonic browser time after the initial elapsed
estimate is anchored to receipt. Terminal spans use their saved finish time.
This is observation time rather than decoding latency.

Defaults: disabled capture, 1,000 terminal requests, seven days, and a 5 GiB
physical storage budget. Counts and budgets are configurable; 1,000 is not a
hard ceiling. Internal safety defaults include a 64 MiB request cap, a 16 MiB
attachment cap, 128 MiB observer memory, a 16 MiB content queue and 1,024 active
capture slots. Supported inline originals have bounded image retention and
lazy previews; effective queue limits can be lower than the attachment cap. Captured
content is distinct from aggregate serving statistics and reusable KV caches.

Each active capture reserves 16 KiB of protected request metadata independently
of content. Settings require the pending-content limit plus all configured
active metadata slots to fit the observer memory budget. A worst-case Unicode/
scalar fixture includes retained requested/serving names and a worker metadata
copy; this structural check does not measure full process RSS. Tight custom
memory configurations may need a smaller active limit or a larger memory budget.

## Source and adoption boundary

- Availability: local focused implementation in progress; no public source
  snapshot is offered for adoption yet.
- Discovery branch: `feature/request-activity`.
- Implementation snapshot for local qualification:
  `8ceaf04f28d5f4ed85a8b114a45919a7b34f2975`. This is a source reference,
  not a release, installation or public-availability claim.
- Baseline: stable `v0.7.1`, full commit
  `bd1e9b112474862ebed550a9c83b4316038a1d74`.
- Current boundary: `omlx/activity/`, Activity settings and credential IDs in
  `omlx/settings.py`, generation endpoint/startup/auth hooks in `omlx/server.py`,
  input/output/lifecycle and dispatch hooks in `omlx/engine_core.py`,
  `omlx/scheduler.py`, `omlx/engine_pool.py`, `omlx/request.py` and
  `omlx/engine/batched.py`, the admin router inclusion,
  the Activity web assets/templates and dashboard links, plus server Activity
  tests and focused web tests.
- Foundation dependencies: Python standard library only. Server and UI wiring
  use the application's existing packages. No downstream scheduling controls,
  browser chat storage, extra database package or hosted service is required.
  Full runtime/composed-build isolation qualification remains pending.

Apply the complete focused change from the baseline through that implementation
snapshot in an isolated target checkout. Later replay or UI commits depend on
the earlier capture, storage and integration work; they are not standalone
features. Preserve the package, shared hooks, web assets, translations and tests
together. Port shared-file conflicts against the selected target deliberately;
do not import a personal integration branch as a prerequisite. No generated app
or native binary accompanies this source.

Capture starts disabled. An administrator opens Activity's capture/settings
control and enables it; existing history remains readable with capture disabled.
The admin session or main API key grants observer access, while named inference
keys identify sources without granting that access. Opening Activity does not
load a model. Disabling capture stops observation rather than inference.

The four required text families (`/v1/completions`, `/v1/chat/completions`,
`/v1/messages`, `/v1/responses`) have source hooks for both HTTP response modes.
Production-route fixtures, synthetic engine output and mocked scheduler/core
paths cover their contracts; full real-model endpoint/backend qualification is
still required. VLM submitted images and lazy previews are implemented, with
actual model/media and broader rendered-browser qualification pending. The evidence
matrix below distinguishes other engine paths from instrumented local text.

The preceding implementation snapshot
`a96413af8ddadef1f112f2edae039aff4617f4bc` passed 1,721 Python checks with one
existing unbuilt native-extension skip, plus 69 Node checks. The current snapshot
changes only web UI assets, its template and regression checks. The JavaScript
and regression snapshot at `7b7037bfa02545d0be57270e29f22f91827a5d17` passed
70 Node checks and two template checks. The mobile and slider-semantics follow-ups
each passed both template checks and focused browser checks; JavaScript and
backend files are unchanged. The backend suite has not been rerun for these UI
snapshots. Initial synthetic browser spot checks covered
desktop light/dark rendering, narrow-screen reselection, elapsed spans, retained
image decoding, far-tail search, navigation, filters, paging, basic live following,
preserved reading position during appends, and a complete synthetic text download.
They do not
establish comprehensive browser, serving-overhead, native-build or private-trial
readiness. Those gates, source publication and catalog/agent-guide reconciliation
remain open.

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
`tests/test_activity_invocations.py` checks structured multi-invocation counts,
frozen/bounded options, coalescing, observation-ended state, resource admission,
writer failure, clear/delete/off fences and transactional v1/v2 migration. Actual
core fixtures additionally check default/overridden parameters without loading
a model or retaining grammar objects.
`tests/test_activity_api_paths.py` reuses the four production APIRoute objects,
original schemas/authentication and exception/response formatters in an isolated
in-memory app. It replaces engine resolution/generation and explicitly emulates
observer callbacks; it does not qualify real engine preprocessing, scheduling,
app lifespan, full middleware composition or real model execution. Cases cover
both response modes, named/main/x-api-key/anonymous-opt-in sources, invalid auth/
JSON/schema exclusion, accepted preflight rejection, multiple completion prompts,
capture-off behavior and generation failures with partial producer output.
`tests/test_activity_routes.py` covers actual admin authorization, bounded reads,
pagination/filtering, snapshot consistency, reconnect/session revalidation,
reader cancellation and stream admission. `tests/test_activity_transport.py`
covers handled exceptions, exact/split UTF-8 ASGI body fragments, failed sends
and responses whose iterators never begin.
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
| `/requests/{id}` | Metadata, bounded invocation details/options, retained representation indexes and version |
| `/requests/{id}/content` | Bounded raw byte ranges for a representation/invocation |
| `/requests/{id}/invocations/{invocation_id}` | One bounded invocation snapshot and admission options; no context BLOB reads |
| `/requests/{id}/attachments/{attachment_id}` | Lazy, bounded-streamed original PNG/JPEG/WebP bytes |
| `/events` | Committed metadata snapshots with selected-request indexes and compact invocation summaries |

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

Live snapshots and history pages accept the same `scope` (`all`, `active` or
`recent`), model/source, state and received-time (`since`/`until`, Unix seconds)
filters. The inspector exposes scope, terminal outcome and a local-time received
range alongside model/source. Invalid or reversed time ranges prevent live-feed
and history-page requests. Changing filters clears old list pages and fences late responses;
selecting a request under unchanged filters preserves the browsed page/cursor.
The global active badge is hidden for a filtered population.

Selected detail remains independent of list filters. A nullable
`selected_outside_filters` flag is evaluated from that exact request row in the
same read transaction; it does not infer exclusion from absence on the first
page. An outside-filter hint appears in the selected inspector, including mobile.
Missing/deleted selection remains distinct from a filtered selection.

The first data event is a `snapshot` with an opaque epoch/channel/sequence ID.
Reconnect accepts `Last-Event-ID`, or an optional `cursor` query when creating a
new EventSource; the header wins. Valid retained history replays contiguously,
one metadata frame at a time. Invalid, foreign, future or missing history yields
`resync_required` before a fresh snapshot. A caught-up new connection receives
the current head. Clients fetch new text ranges from retained content. Session
authorization is rechecked throughout a stream; `authorization_lost`,
`unavailable` and `reconnect` are explicit terminal stream events. Data responses
are marked no-store. Replay contains sampled replacement metadata, rather than
a lossless inference event log or recovered context content.

The replay cache in `omlx/activity/replay.py` retains at most 2 MiB/128 events
globally, 64 events and 1 MiB plus framing per channel, 16 channels and eight
active leases. Events expire after 30 seconds and inactive channels after 60
seconds, with earlier eviction under pressure. These bounds do not promise a
minimum replay window or total process RSS. Active channel locks serialize each
committed read through publication; each viewer follows its own cursor. A fresh
missing selection bypasses historical selected detail. Service/source changes
invalidate old reads and replay identities. The browser carries its last applied
cursor across unchanged-channel network reconnects, compares uint64 sequences
without floating-point conversion, and ignores duplicates or older frames.
`resync_required` starts a fresh baseline on the same stream. Selection/filter,
control, authorization and hidden-inspector rebuilds use a fresh head; history
pagination has its own independent cursor. Malformed or unannounced foreign
identities permit one delayed fresh-head retry per consecutive failure. Run
buffer checks with
`python -m unittest discover -s tests -p test_activity_replay.py -v`. See the
[buffer decision](../decisions/request-activity-replay-buffer.md) for bounds and
failure semantics.

Live snapshot publication is fenced across Activity settings, clear and delete
operations until their actual recorder writes finish, including writes whose
HTTP caller times out or disconnects. Reads spanning a mutation or source/writer
switch are discarded and the stream reconnects. The bounded fence fails closed
on an ambiguous or cancelled writer Future. Focused synthetic cases are in
`tests/test_activity_publication_fence.py`; these do not qualify replay delivery.

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

Invocation events retain the scalar sampling subset for raw evidence inspection.
Structured details now appear in `invocations` on the request-detail endpoint:
independent engine ID mapping, observed model ID at admission, phase/outcome,
prepared count/text availability, generated count, first token/text offsets,
admission/prepared/prefill/end offsets and finish reason. Time offsets use the
request receipt as their common monotonic origin. A finished observation with no
retained engine terminal update becomes `observation_ended`, preserving the last
observed phase without inventing an engine outcome. Missing or stale detailed
snapshots remain possible; request-wide protected counters are separate.

Admission options cover named CPU scalars in `SamplingParams`, bounded stop lists,
and booleans for grammar/logits-observer presence. Objects are never copied or
serialized. Stop strings retain at most 32 entries and 1,024 combined characters;
stop token IDs retain at most 32 entries. Invalid/oversized known values are
listed in `omitted_option_fields`. Options describe core admission, not final
scheduler/kernel configuration, later SpecPrefill defaults, compiled grammar
content or all optional-engine parameters. Actual defaults and overrides are
observed without re-tokenization. Universal pre-parser output and later sparse/
requeue/attention transformations remain qualification work.

Schema 3 transactionally adds request-owned invocation rows to schemas 1/2. Older
history remains readable before migration; newer schemas are preserved/refused.
Each of at most 256 rows has at most 16 KiB of JSON. Updates are immutable, encoded
and merged only on the worker, and share the byte/item/request capture budgets.
A conservative 128 KiB queue reservation covers each snapshot and writer merge
buffers; live observer state reserves 2 KiB against total observer memory until the
observer object is released, including after clear/off/persistence failure.
New capture/content/submitted-snapshot admission includes those live reservations.
When global memory cannot admit another observer, it records an explicit gap.
Request-size admission conservatively counts all accepted update traffic even
though storage keeps only the latest row. Stored captured-byte totals count the
resident row, adjusted transactionally on replacement. Retention/deletion cascade.

Live output snapshots coalesce to at most one per 500 ms per invocation, sampled
when output arrives; lifecycle boundaries attempt immediate snapshots. There is
no per-token SQLite write for invocation details. Queue refusal records a gap and
does not erase existing protected request counters or cancel inference. Detailed
terminal state is budgeted rather than promised lossless. The existing producer
text path has its own content cadence; controlled serving overhead is unqualified.
See [the snapshot decision](../decisions/request-activity-invocation-snapshots.md).

Known scheduler pauses and retries annotate the existing observed generation.
Reasons are bounded to prefill eviction, memory requeue, generation overflow,
cache recovery, batch-reset requeue and bounded memory retry. A repeated pause
within one outstanding cycle is idempotent. Resume is observed at successful
admission/insertion or actual prefill advancement, including packed companions;
queue membership and eviction callback completion do not count as resumed work.
Annotation failures are ignored by scheduling, and disabled/unobserved requests
add no progress maps or scans. Existing scheduler decisions and caches are unchanged.

Details and compact summaries retain the last known phase, observed pause/resume
counts, a fixed reason and receipt-relative monotonic timing. Terminal completion
closes the paused flag without inventing a resume. Stopped observation exposes a
last-observed pause with current state unknown. The selector and selected-generation
readout scope these facts to that generation, so a paused sibling never labels
the entire request as paused. Retry keeps the same invocation ID; repeated prompt
preparation still reports its existing capture gap rather than fabricating a new
effective context. Fixed observer/snapshot resource fixtures and synthetic scheduler
transition checks cover this source boundary; real-backend qualification remains.

SSE includes only compact phase/count/timing summaries and declares
`invocation_options_included:false`; full detail declares it true. Engine IDs,
stop strings and option payloads are excluded from SSE/list delivery. Detail reads
use the same short transaction as request/index metadata, without reading content
BLOBs, and have a fixed 256-row/16 KiB-per-row ceiling. A single-invocation
endpoint reads at most one row under the same admin authority, isolated read
admission and no-store policy. Unknown, noncanonical or missing IDs return 404;
retained history remains readable with capture disabled.

On SQLite builds with JSON functions, live summaries project only their allowed
fields inside the read transaction, so Python does not decode rich admission
options for each poll. Missing values, nulls, booleans and zeros remain distinct.
Older SQLite builds use the existing bounded decode path. Detail reads retain
their full options. This reduces materialized metadata in that path; serving
CPU, process memory and inference overhead still require measurement.

The inspector defaults to Entire request. Its Generation selector lists up to 256
IDs from compact snapshots and retained content indexes, including content whose
rich snapshot was dropped. Selecting a generation recreates the bounded reader
worker with only that invocation's prepared input, producer output and token-ID
indexes, leaving the request-wide counters above unchanged. Search, navigation,
copy and export then cover that filtered evidence. Submitted input and API wire
are request-wide; choosing those views returns to Entire request. Returning
explicitly restores the previous full-request position/mode/follow preference.
Session reading positions retain the invocation selection. Downloads include
the generation ID in their filename.

The inspector header copies the full request ID or a canonical Activity link.
Each message/section heading can copy its complete retained worker text,
including content outside the mounted viewport; missing section keys fail
explicitly. Whole-view copy/download remains available. Clipboard failures fall
back to a text download. Pending exports retain request/view/generation identity
and stop on selection changes, hiding, deletion or lost authorization. Ordinary
live metadata updates do not cancel same-view copies. Old-mode worker renders
cannot replace the current view. Rendered-browser clipboard behavior is still
unqualified.

Admission options load only when expanded, through one single-invocation read;
there is no options polling or retained multi-invocation options cache. One
bounded detail stays mounted, with explicit missing/error/retry and omitted-field
labels and inert text rendering. Changing request/generation, collapsing, hiding,
authorization loss or removal invalidates/aborts that detail read. Live scalar
summaries take precedence over loaded detail for counts/phases. Deferred exports
and scroll restoration are fenced when selection changes. Expanded panels scroll
within bounded heights, while a minimum context height and inspector overflow
keep content reachable on short screens. On mobile, selected detail expands with
normal page scrolling; the virtualized reader keeps a separate viewport between
264 and 600 CSS pixels, using 55% of the dynamic viewport height within those
bounds. The inspector itself adds no nested scroll area in this layout. List-only
mobile scrolling and desktop sizing are unchanged. Synthetic browser checks at
390 by 844 and 375 by 667 CSS pixels verified 464- and 367-pixel readers, no
horizontal page overflow, evidence expansion and back/reselection. Broader touch,
keyboard, text-selection and accessibility qualification remains pending.

The controller checks selection isolation, restored reading position, lazy reads,
stale results, hidden/auth/deletion fences and invalid IDs; the exact filtered
payload is also exercised through the actual reader worker and exported. Real
route checks reject unauthenticated/inference-only keys, deny context/attachment
table reads during the single-row query, and cover disabled/deleted history and
malformed IDs. Template expressions/rendering pass; browser visual/runtime review
is still required.

## Writer collection and payload coalescing

A normal data wake has a 50 ms collection window on the existing worker thread.
Flush, clear, delete, settings changes and shutdown signal a separate urgent event
to bypass that delay. They still wait for an already running SQLite operation;
this is not an absolute I/O deadline. Decode/API producers only offer immutable
observations and never wait for the collection window, compression or storage.

The worker plans each window of at most 64 queued pieces once and joins matching
`raw_output` or `api_response` pieces up to a conservative 64 KiB encoded bound.
The key includes capture handle/epoch, representation, invocation and payload
type. Independent channels may interleave in the queue, but each channel keeps
its byte order. Semantic/event/token/image/invocation chunks and mixed payload
types in the same channel are merge barriers. Large pieces use the existing
bounded encoder. SQLite sequence numbers describe stored chunks, not one-to-one
producer callbacks or global cross-channel causal order.

Planned singleton descriptors prevent repeated scans of the same unique channels.
A deterministic 256-channel case reproduced 42,848 representation-property reads
before this planning rule; its regression bound is now fewer than 3,072, without
using elapsed-time assertions. This is a planner-work check, not a serving benchmark.

Original byte/item/request reservations remain charged while a group is in
flight. A separate conservative 320 KiB scratch reservation covers merge/encoding/
compression buffers; new capture, content, submitted-snapshot and invocation
admission include it in the memory ledger. Without scratch capacity, ordinary
writes proceed without merging. Clear/delete and persistence failure do not
release buffer credits while a valid write still owns them. `writer_scratch_bytes`
in capture health exposes the current buffer reservation. Prepared descriptors
are slotted/weak-referenceable and retain a separate 2 KiB credit each after the
buffer reservation ends; at most 64 exist per window. `prepared_group_bytes`
exposes those credits. They share the 320 KiB reservation while planning/merging
and remain included in every ingress memory guard afterward. A prepared group
rechecks buffer capacity before joining; if capacity has gone, it writes its
original pieces without replanning, new split descriptors or content loss.
Queue and memory settings,
request-byte admission, retained logical-byte totals and capture defaults are
unchanged.

This coalesces payloads before existing storage admission, rather than placing
pruning, vacuum or checkpoint calls inside a long outer transaction. Existing
physical/page-budget and terminal metadata rules still apply. Changed active
phase/count metadata can commit even when another payload is queued. Terminal
metadata and capture removal still wait for the request's queued payloads to
drain; this does not promise a deadline during a blocked storage operation.
Twenty-two deterministic
writer checks cover interleaved channel/request/invocation isolation, byte/item/
type limits, semantic barriers, no-memory fallback, urgent controls/shutdown,
in-flight clear, scratch-aware admission, prepared-capacity changes, descriptor
size/weak-reference lifetime, failed-writer reference release and active metadata
freshness with its paired terminal drain barrier.
A refused combined write records a gap for that group; gap counts do not count
producer pieces. A small burst fixture reduces ten pieces to two actual writes with identical
retained bytes. This establishes fewer writes in that fixture, not measured
inference overhead, high-cardinality CPU cost or a serving-throughput guarantee.
See [the writer decision](../decisions/request-activity-writer-coalescing.md).

## API body observation boundary

With the Activity transport middleware present, `api_response` is captured once
from HTTP body messages at its ASGI send boundary. This includes handled
preflight exceptions whose response is constructed after the endpoint raises.
The endpoint wrapper supplies body capture only when no transport observer is
present, preserving direct-use behavior without duplicate capture. Both valid
JSON and streamed production routes are checked byte-for-byte against client
responses in the isolated protocol fixture.

These are outgoing body attempts at the observation layer, not proof of client
receipt or a physical/compressed network trace. A failed send preserves observed
bytes but reports interrupted transport. Existing bounds, capture-off/delete
fences and protected lifecycle rules remain. Auth failures and malformed schema/
JSON bodies never create a capture in the first place.

Starlette's outer unhandled-error response can bypass user middleware after it
has finalized the failed request. That body's evidence remains unavailable; the
observer does not reopen terminal/deleted captures to append it. The production
outer error handler preserves an existing Activity request ID in its response
header for correlation. Sanitized generic 500 content and capture-off behavior
remain unchanged. Handled errors and streamed formatter failures have actual
wire evidence; this is not a promise that every framework-generated response is
visible. See [the body-observation decision](../decisions/request-activity-api-body-observation.md).

## Backend and evidence coverage

The inspector's expandable Evidence coverage panel separates resolved engine
identity from recorded execution signals. Resolution records only a bounded set
of known kinds: local batched text, batched VLM, local diffusion, distributed,
Splash and other/unknown. An unresolved or historical record stays unclassified.
Multiple resolutions retain distinct kinds in order without retaining engine
objects, addresses, local paths or arbitrary class names. Unknown subclasses do
not inherit a claim to a known implementation merely from their class hierarchy.
This observation is made after normal resolution; model selection and lease
behavior are unchanged.

Detail metadata includes `coverage.backend_kinds`, the resolution source, whether
the shared scheduler was observed, and states for submitted input, prepared text,
admitted token IDs, producer output, API wire and original images. `retained`
means at least that representation exists, not that it is complete. Known scalar
observations with no retained payload are `observed_not_retained`; otherwise the
state is `not_observed` (no recorded signal, not proof the engine never had it).
Capture gaps, browser projection limits and normal lifecycle indicators remain
separate. No backend identity implies universal pre-parser output or a fully
reconstructed attended context.

| Resolved implementation | Evidence boundary |
| --- | --- |
| Local batched text | Shared-core prepared-input/IDs and producer hooks; exact retained fields still determine availability |
| Local batched VLM | Normal shared-core path can observe IDs/output; preprocessing can leave token IDs without prepared text, and no embedding/KV reconstruction is implied |
| Local diffusion | Separate generation path; submitted/API wire where captured, with no shared-core guarantee |
| Distributed | RPC paths can bypass local core; partial chat may use a local fallback, which must be established from actual scheduler signals |
| Splash | Native proxy path; submitted/API wire where captured, with no local-core guarantee; plain completions remain unsupported |
| Other/unresolved | No inferred backend capability; retained evidence is still inspectable |

`tests/test_activity_coverage.py` checks actual class dispatch through normal
leased/unleased resolution with no model load, bounded immutable metadata,
unknown subclasses, proxy identity alongside local-core evidence, VLM ID-only
input and content loss versus unavailable signals. These fixtures qualify the
classification/recording contract; real backend execution, all endpoint modes
and full composition still require qualification. The panel's controller and real
template have local behavior/render checks; comprehensive browser review remains
pending. Duration, count and representation-provenance explanations live in this
disclosure, while capture gaps and projection availability stay visible outside it.

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
The reader worker is addressed through the stable inspector component root,
including when request selection originates from a nested list/button scope.
Elapsed spans match loaded request IDs across framework proxy wrappers. Metadata
and status foregrounds use Activity-scoped theme mixes; representative light/dark
contrast checks passed, without implying a complete accessibility audit.

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
that anchor unless Follow output is selected. The outline pages through the
whole worker index with at most 128 metadata-only entries at once. Paging does
not scroll the document; selecting an entry resolves its stable key against the
current index. Scrolling, rail jumps, search and previous/next navigation reveal
the corresponding outline page; live appends preserve a manually browsed page.
A Message/block ordinal names the current position in the full section index,
which also includes tool definitions, options and raw evidence blocks.
The semantic rail declares a vertical slider with a current section label.
Home and End move to first input and followed output; Up and Down navigate
sections. Synthetic browser checks verified those keys, a visible focus outline,
and keyboard opening/selection in the message outline. This does not qualify
all screen-reader, touch or text-selection behavior.

The rail covers all indexed rows with at most 256 aggregate segments. Grouped
segments report their section range/count and mixed roles explicitly; previews
are bounded to 128 characters. Full bodies and sparse line indexes stay in the
worker. Search rail marks remain capped at 500, with full match counts retained.
Synthetic checks cover over 4,100 sections and a 2 MiB section without full
bodies in outline/rail updates; rendered-browser interaction remains unqualified.

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

- Qualify real-browser reconnect and backpressure behavior against the bounded
  sequenced replay routes and browser cursor handling. Synthetic route checks
  cover initial/poll serialization, independent
  channels, shared-head delivery, source changes, session revocation during reads,
  missing selected detail and restart invalidation.
- Qualify invocation selection/options in a real browser and detailed snapshots
  under load. Final backend options and richer cache evidence
  and all backend/endpoint qualification remain. Backend
  resolution/evidence labels are implemented; they are not qualification badges. Current
  generation hooks are covered by mock-model checks, not real-model validation.
- `raw_output` currently means engine-producer text before API formatting.
  Engines with their own native parsers may already have transformed it;
  universal pre-parser raw generation is not yet qualified.
- Complete browser qualification of the inspector, navigation, dashboard links
  and live controls beyond the initial synthetic spot checks. Qualify larger
  original-image previews and browser pixel budgets;
  supported retained images use lazy previews and unsupported media remain inert.
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
- A small retained PNG was decoded through the actual preview route in the
  browser. Browser pixel-decoder memory, repeated
  large-image throughput and native restart/long-running viewer lifecycle remain
  to be qualified. The first source slice now includes supported inline previews.
- Actual rendered-browser validation, text selection and message-copy interaction
  across virtualized windows, paged outline/aggregate rail navigation and
  deep-link history polish remain to be qualified.
  Native browser Find sees only mounted text; use the inspector search instead.

No source publication, packaged binary, compatibility guarantee or upstream
submission is implied by this in-progress recipe.
