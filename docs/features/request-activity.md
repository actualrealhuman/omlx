# Activity request observability

Feature ID: `request-activity`

## Behavior and use

Activity is being implemented as an opt-in observer for current requests and
bounded recent context history. The current source contains the capture/storage
foundation, settings persistence, request lifecycle and initial engine hooks.
The four generation API families capture accepted semantic input and their API
response stream when explicitly enabled. Local scheduler/core hooks additionally
capture prepared input/tokens and producer output. Admin read endpoints and
snapshot-based live delivery are present; the Activity browser interface is not
available yet. This is not a complete feature
or an independently qualified adoption snapshot.

The foundation uses one background SQLite writer and separately bounded content
and lifecycle metadata. Producer methods enqueue immutable CPU observations;
encoding, compression and persistence happen on the worker. Queue saturation
records incomplete capture while preserving request terminal status. A disabled
recorder creates neither a database nor a worker.

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
  plus `tests/test_activity*.py`.
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
`tests/test_activity_routes.py` covers actual admin authorization, bounded reads,
pagination/filtering, snapshot consistency, reconnect/session revalidation,
reader cancellation and stream admission. `tests/test_activity_transport.py`
covers handled exceptions and responses whose iterators never begin.

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

## Limitations and remaining work

- Complete richer capability reporting, invocation metadata/timings,
  precise prefill/paused state and all backend/endpoint qualification. Current
  generation hooks are covered by mock-model checks, not real-model validation.
- `raw_output` currently means engine-producer text before API formatting.
  Engines with their own native parsers may already have transformed it;
  universal pre-parser raw generation is not yet qualified.
- Implement the inspector, long-context rail/navigation/search, dashboard links,
  translations and admin settings. Assess image support without delaying the
  core text MVP unnecessarily.
- Qualify physical-budget behavior under disk-full conditions, pinned SQLite
  readers, reclamation/migration and very large histories. Current page limits
  conservatively reserve part of the budget for WAL/sidecars; configured disk
  bytes are not a promise of equal logical content capacity.
- Qualify hot-path overhead, worker memory, faithful raw/parsed output,
  cancellations, all required API modes and optional-engine boundaries.
- Validate a fully composed private build separately from the focused source.
- Semantic input export currently runs once at API ingress before preprocessing;
  its cost on very large payloads and media normalization still need qualification.
- Live settings, clear/delete HTTP controls, attachments, inspector search and
  the native restart/long-running viewer lifecycle remain to be completed or
  qualified. The current admin interface is read-only.

No source publication, packaged binary, compatibility guarantee or upstream
submission is implied by this in-progress recipe.
