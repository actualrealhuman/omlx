# Bounded metadata replay storage

The Activity replay buffer stores immutable, fully framed `snapshot` SSE bytes.
It is currently an unused component: live routes still offer `snapshot_only`
delivery. Route and browser integration must establish mutation, authorization
and publication ordering before this buffer can change that contract.

Each channel is keyed by a fixed SHA-256 digest of the eventual normalized
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
gaps. They do not qualify live reconnects, browser behavior or inference latency.

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
recalled. Replay route integration remains a separate required step.
