# 0002: Chat storage redesign — IndexedDB, media preservation, and recovery

- Status: Proposed
- Decision date: 2026-09-23
- Extends: `0001-chat-history-storage-safety.md`

## Context

`0001` stopped automatic deletion but deliberately left the storage backend
unchanged. The remaining limitations are structural:

- **Capacity.** History lives in one `localStorage` value with a per-origin
  quota measured in single-digit megabytes.
- **Attachments are already lost.** `saveCurrentChat` replaces every
  `image_url` and `file` payload with an empty string before persisting, and
  `hasStrippedImages()` / `getStrippedImageCount()` surface the loss in the UI.
  This is a correctness problem, not only a convenience one: a resumed
  conversation forwards empty multimodal parts to the model, and
  "regenerate with a different model" cannot work at all, because the new model
  has no pixels to read.
- **Write amplification.** Every save re-serializes the entire history, so
  appending one message costs the whole corpus.
- **No cross-tab signal.** `localStorage` fires a `storage` event across tabs;
  IndexedDB does not. Concurrent tabs overwrite each other silently today, and
  the backend swap does not fix that by itself.

### Constraints

The built-in chat is a quick local chat with the model. It is not a
multi-client or multi-user chat system, and it should not grow into one. Users
who need accounts, sync, review workflows, or large durable corpora should run a
purpose-built system. This constraint is deliberate and shapes every decision
below.

## Decision

### Backend

IndexedDB, scoped per browser and per device. Not server-side. The local
backend remains compute-only and holds no chat state.

### Schema — three object stores

```
chats   chatId : { title, model, createdAt, updatedAt, rev, bytes, messages[] }
blobs   digest : { blob, mediaType, size, width, height, createdAt }
meta    backend marker, schema version, migration state
```

Separating blobs from messages is the primary capacity and performance win:
text stays small and fast, media stays large and lazy.

Splitting `messages` into its own store is deferred. Per-chat records already
remove whole-history rewrites; per-message records add indexing and range-query
complexity for a gain that only matters once a single chat is very large.

### Media

**Preserve full originals in all cases.** Do not substitute thumbnails, do not
downscale on save, and do not silently drop attachments to stay under quota.
Storage exhaustion is surfaced to the user, who decides what to delete — the
same principle as `0001`.

Store a **derived thumbnail alongside each original**, used only for rendering
history and lists. Thumbnails are a render optimization, not a storage
compromise: regenerate and continuation always use the original.

### Content addressing

The SHA-256 digest of the raw bytes is the blob's identity.

- Deduplicates automatically in storage and again inside export archives.
- Makes import dedup trivial: a blob already present is skipped and message
  references resolve to it with no ID remapping.
- Gives import an integrity check by re-hashing against the manifest.

**Hash at upload time, never inside a write transaction.** IndexedDB
transactions close if a non-IndexedDB promise is awaited within them, so a
`crypto.subtle.digest()` call inside a save would silently abort the write.

**Content addressing requires a garbage collector.** Deleting a chat must not
leave its blobs behind, and dedup makes leaks worse because one blob may be
referenced by several chats. Use mark-and-sweep: collect referenced digests from
the message store, delete unreferenced blobs. Run it as an explicit, user-
initiated maintenance action that reports what it found and freed — never as a
silent background eviction.

### Write model

**Await saves.** Persistence completes before the caller proceeds.

The performance argument for background buffering disappears once per-chat
records remove whole-history rewrites: a single chat write is small and fast.
Awaiting also keeps the `0001` safety layer unchanged, since it already assumes
a synchronous save-and-check, and preserves its existing test coverage.

Large media writes may take a visible moment; show progress for those. Typing
and text saves must not block.

### Multi-tab

**Optimistic concurrency control.** Each chat record carries a `rev` integer
bumped on every write.

```
load chat  → remember rev
save chat  → in one readwrite transaction:
               read current rev
               unchanged → write with rev + 1
               changed   → abort, surface the recovery banner
```

The banner offers "load the newer version" or "keep mine and overwrite." This
reuses the `0001` recovery UI and the rule that only the user deletes.

Atomic read-modify-write is something `localStorage` cannot provide; IndexedDB
gives it for free, so the migration improves multi-tab safety rather than
degrading it.

Add a soft advisory via `BroadcastChannel` when a second tab is open. Do **not**
use leader election — it produces confusing read-only states, and the `rev`
check already prevents actual loss.

### Storage meter

`navigator.storage.estimate()` provides the origin-wide headline number only.
The actionable breakdown must come from app metadata so the meter never has to
read every chat:

- `blobs.size` is a file stat and free
- `chats.bytes` is stamped at save time

Present media and text separately, with the largest items listed and individually
deletable, and request `navigator.storage.persist()` so the browser stops
evicting under pressure.

Deletion actions must show their work: what will be removed, how much it frees,
reference counts, a backup offered beforehand, and the space actually reclaimed.

### Export and import

Two export modes: **chats only** and **chats + media**.

```
omlx-export-<date>.zip
├── manifest.json     schema version, app version, chat records, digest index
└── media/<digest>.<ext>
```

- Media stored with `STORED`, not `DEFLATE` — image formats are already
  compressed; recompressing costs CPU and saves nothing.
- Digest as filename, lowercase hex, real extension so files are usable outside
  the app.
- Import verifies by re-hashing against the manifest.
- Import **merges by default**; replace is an explicit advanced action behind a
  typed confirmation.
- New chats use **UUIDs** so merge collisions are rare by construction.
- Partial import is safe by construction: blob writes are idempotent, so media
  streams in first, digests verify, chat records commit last, and any orphans
  are swept.

Export is performed in the browser. Streaming through the local backend was
considered and rejected: it reintroduces server involvement for sizes outside
this feature's intended scope.

### Scope boundary

Per-device, per-browser. No sync, no accounts, no conflict resolution beyond the
`rev` check, no server-side retention. Stated explicitly so the IndexedDB choice
reads as correct for the product rather than as an unfinished step toward
something larger.

## Consequences

- Attachments survive reload, and cross-model regenerate becomes possible.
- Capacity moves from megabytes to hundreds of megabytes or more, and eviction
  stops being a silent data-loss vector.
- Whole-history rewrites and the weak multi-tab behavior called out in `0001`
  are resolved rather than merely documented.
- Backup can no longer be a bare JSON file; the archive format is a required
  part of this work, not a later addition.
- Vision-token accounting must enter context budgeting once images persist, or
  context usage will be under-reported on image-bearing conversations.
- A garbage collector becomes a permanent subsystem with its own failure modes.
- Existing stripped attachments are unrecoverable. Migration preserves data from
  install forward and resurrects nothing retroactively.

## Migration

1. Detect a legacy `localStorage` history value and validate it before reading
   further.
2. Open IndexedDB and write per-chat records and any recoverable blobs.
3. Verify the committed result — record count and byte totals, not merely the
   absence of an exception.
4. Write the backend marker only after verification.
5. Retain the legacy value as a backup. Remove it only through an explicit user
   action.
6. On any failure, remain on `localStorage`, surface the `0001` banner, and keep
   the application usable.

Migration must not run before `0001` is in place. Without the safety layer, a
failed migration on a codebase that still silently evicts is strictly worse than
either change alone.

## Phasing

| Phase | Scope | Independently valuable |
| --- | --- | --- |
| 1 | `0001` safety fix | Yes — stops data loss, no backend change |
| 2 | Per-chat records, with the storage abstraction folded in | Yes — removes write amplification |
| 3 | IndexedDB backend, schema, migration | Yes — capacity and durability |
| 4 | Media preservation, meter, export/import, multi-tab `rev` | Yes — restores attachments |

A pure abstraction change is not submitted on its own; it travels with the phase
that needs it.

## Open questions

- Should phase 4 be split further, given media, meter, and export are each
  sizeable?
- What is the right proactive quota warning threshold, if any?
- Should `navigator.storage.persist()` be requested automatically or only from
  the storage panel?

## References

- `0001-chat-history-storage-safety.md`
- `tests/chat_history_storage.test.cjs`
- `tests/test_chat_ui_overhaul.py`
