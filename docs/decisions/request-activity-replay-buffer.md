# Bounded metadata replay storage

The Activity replay buffer stores immutable, fully framed `snapshot` SSE bytes.
Admin live routes use it for bounded metadata replay. The browser's explicit
cursor and duplicate handling remains a separate integration step.

Each channel is keyed by a fixed SHA-256 digest of the normalized
selection, with opaque epoch/channel IDs and bounded sequence numbers. The
buffer retains no HTTP requests, credentials, sessions, readers or content
bodies. A short synchronous lock protects its metadata; it performs no database,
network or asynchronous work. Sharing replay frames never places a browser on
the inference producer path.

Encoded cache storage is capped at 2 MiB and 128 events globally, 64 events and
1 MiB plus framing allowance per channel, 16 channels and eight active leases.
Frames expire after 30 seconds; unleased channel metadata expires after 60
seconds or earlier under capacity pressure. These are maximum retention bounds,
not a promised replay duration or a bound on total process RSS. Active leases
pin channel identity, but do not prevent event eviction.

Replay returns one contiguous frame at a time. Missing, foreign, future or
invalid cursors require resynchronization. Re-observing identical payload bytes
does not advance the sequence. If the current frame has expired, the caller's
fresh payload may be framed again without restoring expired history or hiding
a gap. Reset and sequence exhaustion invalidate leases and release all owned
history without accumulating tombstones.

Focused standard-library tests cover framing, deduplication, byte/count/lease
caps, TTL and LRU eviction, invalid input atomicity, reset, overflow and replay
gaps. Route checks additionally cover committed-read/publication ordering,
shared viewers, missing selections and lifecycle invalidation. These synthetic
checks do not qualify real-browser reconnects or inference latency.

## Route ordering and delivery

The route uses `epoch:channel:sequence` cursors. `Last-Event-ID` takes precedence
over the optional bounded `cursor` query. A missing, invalid, foreign, future or
expired cursor produces `resync_required` before a newly read authoritative head.
A caught-up new connection receives a current head to initialize its view.

At most eight active channel coordinators hold loop-owned async locks. Each
channel's initial and subsequent committed read plus sequence publication share
that lock. Authorization, network delivery and sleeps happen outside it. Only
active coordinators exist; response finalization releases their leases even if
the body never starts. A different event loop is rejected while coordinators
remain active; an idle rebind resets the epoch. Service restart invalidates both
cached history and responses that have not started their body yet.

Each viewer follows its own last-delivered cursor, including changes already
published by another viewer. Contiguous history drains one frame at a time,
without a per-viewer queue. A generator retains one outgoing metadata frame;
reader/encoder work and ASGI transport transients, including the previous sent
chunk, are separate from the encoded-cache budget. No total-RSS claim follows
from that budget.

A fresh missing selected request bypasses old selected history. Once a stream
observes that absence, it uses fresh heads rather than historical detail frames.
This does not revoke bytes already sent or make retention observation immediate.

## Publication fencing

Current snapshot delivery also fences admin history/settings mutations. A worker
timing out while awaiting SQLite does not prove that its write has finished.
Publication remains blocked until both the actual executor operation and its
recorder Future have completed. HTTP cancellation cannot release this gate early,
including cancellation while the executor operation is still queued.

The fence retains at most 16 tokens with fixed scalar state, without retaining
HTTP contexts or writer Futures. Ambiguous or cancelled writer Futures keep the
gate closed; capacity exhaustion rejects further mutations before writing.
Ordinary completion, including a reported writer failure, changes the publication
generation. Source/writer identity checks additionally discard reads spanning a
source switch. A stream validates its initial generation after reads and async
authorization and before publishing snapshots or keepalives; invalidation ends
the stream with `reconnect`. Bytes already handed to the network cannot be
recalled. Revalidation occurs after reader and lock waits, immediately before
each metadata frame or keepalive is published.
