# 0002: Chat storage redesign — IndexedDB, media preservation, and recovery

- Status: Accepted
- Decision date: 2026-09-23
- Implementation status: phases 2 and 3 implemented on local branches
  `feature/chat-storage-records` and `feature/chat-storage-indexeddb`; phase 1
  is `0001`; phase 4 not started. Nothing published.
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
Large media writes may take a visible moment; show progress for those. Typing
and text saves must not block.

**The store interface is asynchronous from phase 2, not phase 3.** The phase-2
`localStorage` backend is synchronous underneath, but the interface returns
promises so that call sites convert to `await` once, in the phase that carries
no backend risk. Deferring the async conversion to phase 3 would put call-site
churn in the same change as the backend swap and the migration, which is the
single riskiest combination in this plan.

The `0001` save-and-check pattern is preserved unchanged: callers still check
the result before proceeding. Only the syntax gains an `await`, which strengthens
rather than weakens the existing guarantee.

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
| 2 | Per-chat records + swappable async store interface | Yes — removes write amplification |
| 3 | IndexedDB backend, schema, migration, multi-tab `rev` locking | Yes — capacity, durability, and multi-tab safety |
| 4 | Media preservation, meter, export/import | Yes — restores attachments |

A pure abstraction change is not submitted on its own; it travels with the phase
that needs it.

Multi-tab `rev` locking was originally listed under phase 4 and is delivered with
phase 3 instead: `rev` is a property of the record and of atomic read-modify-write,
so it belongs with the backend that provides that atomicity rather than with media
handling.

## Resolved questions

**Should phase 4 be split further?** Yes — into two, and they are now named so
the sequencing is explicit:

- **4a — media preservation**: populate the `blobs` store, stop stripping
  `image_url`/`file` parts, derive thumbnails at upload, and land the
  mark-and-sweep collector. This is the correctness fix and it stands alone.
- **4b — meter and archive**: the storage panel, `estimate()` plus
  metadata-backed breakdown, and the two-mode export/import with digest
  verification.

Splitting matters because 4a changes what a message contains, which changes what
an archive must carry. Doing them together couples a correctness fix to a
feature, and the correctness fix is the one worth landing first.

**Proactive quota warning threshold?** Deferred to 4b, and deliberately so. A
warning that says "storage is nearly full" with no way to see what is using it
or to free it is noise, and noise trains users to dismiss the banner that
protects them. The warning ships with the meter, not before it. Exhaustion
already surfaces loudly through the `0001` banner, which is the right behaviour
until there is something actionable to say.

**`navigator.storage.persist()` automatic or panel-only?** Panel-only. It is a
request for elevated storage and prompting for it on first load is presumptuous
for a quick-chat feature. Offer it in the storage panel with a plain explanation
of what it prevents, and show its current state. Phase 3 does not call it.

## References

- `0001-chat-history-storage-safety.md`
- `omlx/admin/static/js/chat_record_store.js` — localStorage backend
- `omlx/admin/static/js/chat_indexeddb_store.js` — IndexedDB backend
- `omlx/admin/static/js/chat_history_migration.js` — legacy and backend migration
- `tests/chat_history_storage.test.cjs`
- `tests/chat_record_store.test.cjs`
- `tests/chat_indexeddb_store.test.cjs`
- `tests/chat_history_migration.test.cjs`
- `tests/chat_backend_migration.test.cjs`
- `tests/test_chat_ui_overhaul.py`

## Testing caveat

The IndexedDB tests run against `tests/helpers/fake_indexeddb.cjs`, a
dependency-free stand-in that models transaction atomicity, abort-rolls-back,
quota errors, and the deferred-handler timing that real IndexedDB uses. It is
not a browser. Upgrade blocking across tabs, real blob storage, eviction
behaviour, and the auto-commit-on-idle rule are only approximated there, so the
migration and the multi-tab locking need a pass in a real browser before they are
considered verified end to end. The interface contract and the compare-and-set
logic are covered by the fake; browser semantics are not.
