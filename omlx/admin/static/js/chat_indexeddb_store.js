// SPDX-License-Identifier: Apache-2.0

// IndexedDB backend for the per-chat record store.
//
// Phase 3 of the storage redesign (docs/decisions/0002-chat-storage-redesign.md).
// Implements the same interface as the localStorage backend, so no caller changes.
//
// What IndexedDB buys here:
//   - capacity measured in hundreds of megabytes rather than a few;
//   - a genuinely atomic read-modify-write, so the revision check is race-free
//     across tabs (the localStorage backend could only approximate it);
//   - the record and its index entry committing in one transaction, so the
//     index can no longer drift from the data and `indexDegraded` disappears;
//   - exemption from the eviction policies that can silently drop localStorage.
//
// Transaction discipline: no non-IndexedDB promise is ever awaited inside a
// transaction. Hashing and other async work happen before the transaction opens,
// because an idle transaction auto-commits and later writes in it would fail.

(function (root) {
    'use strict';

    const DB_NAME = 'omlx-chat';
    const DB_VERSION = 1;
    const CHATS = 'chats';
    const CHAT_INDEX = 'chatIndex';
    const META = 'meta';
    const BLOBS = 'blobs';

    function toError(error, name, message) {
        if (error && typeof error === 'object') {
            if (!error.name) error.name = name;
            return error;
        }
        const wrapped = new Error(message || name);
        wrapped.name = name;
        return wrapped;
    }

    function reqToPromise(request) {
        return new Promise((resolve, reject) => {
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(toError(request.error, 'IndexedDBRequestError'));
        });
    }

    function txDone(tx) {
        return new Promise((resolve, reject) => {
            tx.oncomplete = () => resolve();
            tx.onabort = () => reject(toError(tx.error, 'IndexedDBTransactionAborted', 'transaction aborted'));
            tx.onerror = () => reject(toError(tx.error, 'IndexedDBTransactionError'));
        });
    }

    // IDBCursor.continue() returns undefined. It does not hand back a new
    // request: it re-fires `success` on the originating openCursor request with
    // the next cursor, and a null result ends the iteration. Driving the loop
    // from that single handler also keeps the transaction continuously busy,
    // which the auto-commit-on-idle rule requires — awaiting across each row
    // leaves a gap in which the browser commits and the next request fails.
    function cursorAll(store, mapper) {
        return new Promise((resolve, reject) => {
            const out = [];
            let request;
            try {
                request = store.openCursor();
            } catch (error) {
                reject(error);
                return;
            }
            request.onsuccess = () => {
                const cursor = request.result;
                if (!cursor) {
                    resolve(out);
                    return;
                }
                try {
                    out.push(mapper ? mapper(cursor.value) : cursor.value);
                    cursor.continue();
                } catch (error) {
                    reject(error);
                }
            };
            request.onerror = () => reject(request.error);
        });
    }

    function byteLengthOf(value) {
        try {
            return typeof value === 'string' ? value.length : JSON.stringify(value).length;
        } catch {
            return 0;
        }
    }

    // Alpine keeps the chat list in deeply reactive Proxy trees, so a record read
    // out of component state is a Proxy, sometimes nested. IndexedDB writes go
    // through the structured clone algorithm, which throws DataCloneError on a
    // Proxy, and the caller then reports the browser as having no storage when the
    // real problem is one object's shape. The tree is walked and Proxies are
    // replaced by plain copies of their own enumerable properties.
    //
    // Deliberately not a JSON round-trip: that would drop functions and symbols and
    // turn every Date into a string, so a record could come back subtly different
    // from what was put, and it would reject the circular references IndexedDB
    // stores without complaint. Anything genuinely uncloneable is left in place so
    // the store rejects it and the caller reports it — the backend must not quietly
    // discard data it cannot take.
    //
    // Cycles survive because each output is registered before its children are
    // filled, so a self-referencing record keeps its shape.
    const MAX_RECORD_DEPTH = 100;
    // Host objects the structured clone algorithm accepts as they are. A Proxy in
    // front of one reports the wrapped tag, so this covers reactive dates too.
    const CLONEABLE_TAGS = new Set([
        '[object Date]', '[object RegExp]', '[object ArrayBuffer]',
        '[object DataView]', '[object Blob]', '[object File]',
        '[object ImageData]', '[object DOMException]', '[object Error]',
    ]);

    const TYPED_ARRAY_TAG = /^\[object [A-Za-z0-9]*Array\]$/;

    function toPlainStructure(value, seen, depth) {
        if (value === null || typeof value !== 'object') return value;
        const tag = Object.prototype.toString.call(value);
        if (CLONEABLE_TAGS.has(tag)) return value;
        // Typed arrays and their proxies clone directly; plain arrays do not.
        if (tag !== '[object Array]' && TYPED_ARRAY_TAG.test(tag)) return value;
        if (depth > MAX_RECORD_DEPTH) {
            throw new RangeError('chat record nesting exceeds ' + MAX_RECORD_DEPTH + ' levels');
        }
        if (seen.has(value)) return seen.get(value);

        if (tag === '[object Map]') {
            const map = new Map();
            seen.set(value, map);
            try {
                for (const entry of value) {
                    map.set(toPlainStructure(entry[0], seen, depth + 1), toPlainStructure(entry[1], seen, depth + 1));
                }
            } finally { seen.delete(value); }
            return map;
        }
        if (tag === '[object Set]') {
            const set = new Set();
            seen.set(value, set);
            try {
                for (const entry of value) set.add(toPlainStructure(entry, seen, depth + 1));
            } finally { seen.delete(value); }
            return set;
        }

        const out = Array.isArray(value) ? [] : {};
        seen.set(value, out);
        try {
            if (Array.isArray(value)) {
                for (let i = 0; i < value.length; i += 1) out[i] = toPlainStructure(value[i], seen, depth + 1);
                return out;
            }
            // A plain object, or a Proxy standing in for one.
            for (const key of Object.keys(value)) out[key] = toPlainStructure(value[key], seen, depth + 1);
            return out;
        } finally {
            seen.delete(value);
        }
    }

    function toStorable(value) {
        return toPlainStructure(value, new Map(), 0);
    }

    // navigator.storage is a secure-context-only API: over plain HTTP on a LAN or
    // Tailscale hostname Chrome leaves it undefined even though IndexedDB works
    // there. It is an enhancement — quota reporting and eviction resistance — and
    // never a precondition for saving chats, so every access goes through here and
    // reports "absent" rather than throwing. persist() is deliberately never
    // called: the ADR keeps that request panel-only.
    function storageManager() {
        try {
            const nav = typeof navigator !== 'undefined' ? navigator : null;
            const sm = nav && nav.storage;
            return sm && typeof sm.estimate === 'function' ? sm : null;
        } catch {
            return null;
        }
    }

    function createIndexedDBRecordStore(options = {}) {
        const idb = options.indexedDB;
        if (!idb) throw new TypeError('createIndexedDBRecordStore requires indexedDB');

        const name = options.name || DB_NAME;
        const version = options.version || DB_VERSION;
        // Resolved at call time, not at module load: chat_media_store.js is a
        // sibling classic script, and in node the test loads it first.
        const codec = options.media
            || (typeof root.createMediaCodec === 'function'
                ? root.createMediaCodec({ crypto: options.crypto })
                : null);
        const media = codec && typeof codec.supported === 'boolean'
            ? codec
            : { supported: false, offloadMessages: null, resolveMessages: null, digestsIn: () => new Set() };
        let dbPromise = null;

        function open() {
            if (!dbPromise) {
                dbPromise = new Promise((resolve, reject) => {
                    let request;
                    try {
                        request = idb.open(name, version);
                    } catch (error) {
                        reject(toError(error, 'IndexedDBOpenError'));
                        return;
                    }
                    request.onupgradeneeded = (event) => {
                        const db = request.result;
                        const oldVersion = event.oldVersion || 0;
                        if (oldVersion < 1) {
                            const chats = db.createObjectStore(CHATS, { keyPath: 'id' });
                            chats.createIndex('updatedAt', 'updatedAt');
                            db.createObjectStore(CHAT_INDEX, { keyPath: 'id' });
                            db.createObjectStore(META, { keyPath: 'key' });
                            // Content-addressed media, keyed by digest. Records hold
                            // a reference and the bytes live here, so an attachment
                            // survives reload and one image shared across chats is
                            // stored once. collectBlobs() reclaims orphans.
                            db.createObjectStore(BLOBS, { keyPath: 'digest' });
                        }
                    };
                    request.onsuccess = () => resolve(request.result);
                    request.onerror = () => reject(toError(request.error, 'IndexedDBOpenError'));
                    request.onblocked = () => reject(
                        new Error('IndexedDB upgrade blocked by another open tab'),
                    );
                });
                dbPromise.catch(() => { dbPromise = null; });
            }
            return dbPromise;
        }

        function classify(error) {
            const name = error?.name || '';
            // A value that cannot be cloned is a shape problem in the record, not a
            // dead store. Labelling it 'unavailable' tells the user their browser
            // storage is gone when the truth is that one field was unserializable.
            if (name === 'DataCloneError' || /could not be cloned/i.test(String(error?.message || ''))) {
                return 'serialization';
            }
            const quota = name === 'QuotaExceededError'
                || name === 'NS_ERROR_DOM_QUOTA_REACHED'
                || /quota/i.test(String(error?.message || ''));
            return quota ? 'quota' : 'unavailable';
        }

        // ---- interface ----

        async function get(id) {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            let record;
            try {
                record = await reqToPromise(db.transaction(CHATS, 'readonly').objectStore(CHATS).get(id));
            } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            if (!record || !media.supported || !Array.isArray(record.messages)) {
                return { ok: true, record: record ?? null };
            }
            // Digest references resolve back to inline payloads so the caller sees
            // the message it stored. A blob that cannot be read leaves its reference
            // unresolved and is reported, rather than making the whole chat
            // unreadable — losing the text of a conversation because one attachment
            // went missing is the worse outcome.
            try {
                const blobStore = db.transaction(BLOBS, 'readonly').objectStore(BLOBS);
                const resolved = await media.resolveMessages(record.messages, {
                    getBlob: (digest) => reqToPromise(blobStore.get(digest)),
                });
                return {
                    ok: true,
                    record: { ...record, messages: resolved.messages },
                    mediaMissing: resolved.missing,
                };
            } catch (error) {
                return { ok: true, record, mediaMissing: [{ kind: 'unavailable', error }] };
            }
        }

        // Atomic compare-and-set. The read of the current revision and the write
        // happen in the same transaction, so two tabs cannot both believe they won.
        //
        // `preserveRev` exists for migration only: it carries the source record's
        // revision forward so a tab still holding the old backend's revision
        // collides here instead of silently overwriting. Ordinary writes never use
        // it — they let the store mint the next revision.
        //
        // Inline media is offloaded to the blobs store and replaced by a digest
        // reference, so `get` can hand back the same message that was put.
        async function put(record, { expectRev = null, preserveRev = false } = {}) {
            if (!record || typeof record !== 'object' || record.id == null) {
                return { ok: false, kind: 'invalid', error: new TypeError('Record requires an id') };
            }
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }

            // Hashing is asynchronous, so it happens before the transaction opens.
            // Awaiting crypto inside a transaction leaves it idle long enough to
            // auto-commit, and the writes that follow would then fail.
            let offloaded = { messages: record.messages, blobs: [] };
            if (media.supported && Array.isArray(record.messages)) {
                try {
                    offloaded = await media.offloadMessages(record.messages);
                } catch (error) {
                    return { ok: false, kind: classify(error), error };
                }
            }
            // Rebuild the candidate as plain data. A record taken from component
            // state is an Alpine reactive tree, and even after the spread above the
            // nested messages array is still a Proxy — which the structured clone
            // inside IDBObjectStore.put() refuses outright with DataCloneError.
            // Doing it here keeps the constraint in the backend instead of in
            // every caller.
            let payload;
            try {
                payload = toStorable({ ...record, messages: offloaded.messages });
            } catch (error) {
                return { ok: false, kind: 'serialization', error };
            }
            const newBlobs = Array.isArray(offloaded.blobs) ? offloaded.blobs : [];

            let tx;
            try {
                tx = db.transaction([CHATS, CHAT_INDEX, BLOBS], 'readwrite');
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }

            const chats = tx.objectStore(CHATS);
            const index = tx.objectStore(CHAT_INDEX);
            const blobs = tx.objectStore(BLOBS);

            try {
                const existing = await reqToPromise(chats.get(record.id));
                const currentRev = Number.isInteger(existing?.rev) ? existing.rev : 0;

                if (expectRev !== null && expectRev !== currentRev) {
                    // Refuse without writing. Aborting rolls the transaction back so
                    // neither store is touched.
                    try { tx.abort(); } catch {}
                    return {
                        ok: false,
                        kind: 'conflict',
                        expectedRev: expectRev,
                        currentRev,
                        current: existing ?? null,
                    };
                }

                // Blobs before record. If the transaction fails after this point the
                // worst case is an orphaned blob, which the collector reclaims; the
                // reverse order could commit a record pointing at bytes that were
                // never written.
                let blobsWritten = 0;
                for (const blob of newBlobs) {
                    const present = await reqToPromise(blobs.get(blob.digest));
                    if (!present) {
                        blobs.put({ ...blob, createdAt: new Date().toISOString() });
                        blobsWritten += 1;
                    }
                }

                const carried = preserveRev && Number.isInteger(record.rev)
                    ? Math.max(record.rev, currentRev)
                    : currentRev + 1;
                const nextRev = Math.max(carried, currentRev);
                const stored = { ...payload, rev: nextRev };
                chats.put(stored);
                index.put({
                    id: record.id,
                    title: record.title ?? '',
                    model: record.model ?? null,
                    createdAt: record.createdAt ?? null,
                    updatedAt: record.updatedAt ?? null,
                    rev: nextRev,
                    bytes: byteLengthOf(stored),
                    messageCount: Array.isArray(record.messages) ? record.messages.length : 0,
                });

                await txDone(tx);
                // Record and index committed together: no degraded-index state.
                return { ok: true, rev: nextRev, blobsWritten };
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
        }

        async function remove(id) {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            let tx;
            try {
                tx = db.transaction([CHATS, CHAT_INDEX], 'readwrite');
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
            const chats = tx.objectStore(CHATS);
            try {
                const existing = await reqToPromise(chats.get(id));
                chats.delete(id);
                tx.objectStore(CHAT_INDEX).delete(id);
                await txDone(tx);
                return { ok: true, removedRaw: existing ? JSON.stringify(existing) : null };
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
        }

        // Reads only the metadata store, so listing the sidebar never pulls
        // message bodies.
        async function listIndex() {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            try {
                const entries = await cursorAll(db.transaction(CHAT_INDEX, 'readonly').objectStore(CHAT_INDEX));
                return { ok: true, entries };
            } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
        }

        async function keys() {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            try {
                const ids = await cursorAll(
                    db.transaction(CHATS, 'readonly').objectStore(CHATS),
                    (value) => value.id,
                );
                return { ok: true, ids };
            } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
        }

        async function rebuildIndex() {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            let tx;
            try {
                tx = db.transaction([CHATS, CHAT_INDEX], 'readwrite');
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
            try {
                tx.objectStore(CHAT_INDEX).clear();
                const entries = await cursorAll(tx.objectStore(CHATS));
                for (const record of entries) {
                    tx.objectStore(CHAT_INDEX).put({
                        id: record.id,
                        title: record.title ?? '',
                        model: record.model ?? null,
                        createdAt: record.createdAt ?? null,
                        updatedAt: record.updatedAt ?? null,
                        rev: Number.isInteger(record.rev) ? record.rev : 0,
                        bytes: byteLengthOf(record),
                        messageCount: Array.isArray(record.messages) ? record.messages.length : 0,
                    });
                }
                await txDone(tx);
                return { ok: true, count: entries.length, corrupt: [] };
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
        }

        async function getMeta() {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            try {
                const row = await reqToPromise(db.transaction(META, 'readonly').objectStore(META).get('app'));
                return { ok: true, meta: row ? row.value : null };
            } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
        }

        async function setMeta(patch) {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            try {
                const tx = db.transaction(META, 'readwrite');
                const store = tx.objectStore(META);
                const row = await reqToPromise(store.get('app'));
                const base = row && row.value && typeof row.value === 'object' ? row.value : {};
                const merged = toStorable({ ...base, ...patch, schemaVersion: DB_VERSION });
                store.put({ key: 'app', value: merged });
                await txDone(tx);
                return { ok: true };
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
        }

        async function totalBytes() {
            const listed = await listIndex();
            if (!listed.ok) return listed;
            let bytes = 0;
            for (const entry of listed.entries) bytes += Number(entry.bytes) || 0;
            return { ok: true, bytes };
        }

        // Explicit user action only. Returns the bytes removed so the caller can
        // report what was reclaimed.
        async function clearAll() {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            let tx;
            try {
                tx = db.transaction([CHATS, CHAT_INDEX], 'readwrite');
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
            try {
                const existing = await cursorAll(tx.objectStore(CHATS));
                const removed = existing.map((record) => JSON.stringify(record));
                tx.objectStore(CHATS).clear();
                tx.objectStore(CHAT_INDEX).clear();
                await txDone(tx);
                return { ok: true, removed };
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
        }

        // ---- media blobs ----

        async function getBlob(digest) {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            try {
                const row = await reqToPromise(db.transaction(BLOBS, 'readonly').objectStore(BLOBS).get(digest));
                return { ok: true, blob: row ?? null };
            } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
        }

        async function listBlobs() {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            try {
                const rows = await cursorAll(db.transaction(BLOBS, 'readonly').objectStore(BLOBS));
                return { ok: true, blobs: rows };
            } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
        }

        // Mark-and-sweep over the blobs store.
        //
        // Mark: every digest referenced by a committed record. Sweep: delete the
        // blobs whose digest is not in that set. Content addressing has no other
        // way to tell a live attachment from an orphan left by a failed save.
        //
        // Both halves run inside one readwrite transaction spanning CHATS and
        // BLOBS. IndexedDB serialises readwrite transactions over the same object
        // stores, so a concurrent put cannot add a reference that this pass then
        // sweeps away.
        //
        // If the mark pass fails the sweep never runs. Deleting against a partial
        // mark set would destroy attachments that are still in use, which is the
        // one outcome this function must not produce.
        async function collectBlobs() {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            let tx;
            try {
                tx = db.transaction([CHATS, BLOBS], 'readwrite');
            } catch (error) {
                return { ok: false, kind: classify(error), error };
            }
            const chats = tx.objectStore(CHATS);
            const blobs = tx.objectStore(BLOBS);

            let referenced;
            let allBlobs;
            try {
                const records = await cursorAll(chats);
                referenced = new Set();
                for (const record of records) {
                    for (const digest of media.digestsIn(record.messages)) referenced.add(digest);
                }
                allBlobs = await cursorAll(blobs);
            } catch (error) {
                // Nothing has been deleted at this point; abandon the sweep whole.
                try { tx.abort(); } catch {}
                return { ok: false, kind: classify(error), error, removed: [], bytesFreed: 0 };
            }

            const removed = [];
            let bytesFreed = 0;
            for (const blob of allBlobs) {
                if (referenced.has(blob.digest)) continue;
                blobs.delete(blob.digest);
                removed.push(blob.digest);
                bytesFreed += Number(blob.bytes) || 0;
            }
            try {
                await txDone(tx);
            } catch (error) {
                return { ok: false, kind: classify(error), error, removed: [], bytesFreed: 0 };
            }
            return {
                ok: true,
                removed,
                bytesFreed,
                kept: allBlobs.length - removed.length,
                referenced: referenced.size,
            };
        }

        async function close() {
            if (!dbPromise) return;
            try {
                const db = await dbPromise;
                if (typeof db.close === 'function') db.close();
            } catch {}
            dbPromise = null;
        }

        return {
            backend: 'indexedDB',
            schemaVersion: DB_VERSION,
            supportsBlobs: true,
            media,
            open,
            close,
            get,
            put,
            remove,
            listIndex,
            rebuildIndex,
            getMeta,
            setMeta,
            keys,
            clearAll,
            totalBytes,
            getBlob,
            listBlobs,
            collectBlobs,
            storeNames: { CHATS, CHAT_INDEX, META, BLOBS },
        };
    }

    // Cheap capability probe, asked of IndexedDB itself. Private browsing and some
    // configurations reject IndexedDB outright, and the caller must fall back rather
    // than assume.
    //
    // Deliberately independent of `navigator.storage` and of
    // `window.isSecureContext`: Chrome leaves both absent/false on a plain-HTTP
    // LAN or Tailscale origin while IndexedDB works perfectly there. Consulting
    // them here would report the store as unavailable on a configuration that is
    // fully supported, so the probe opens a database and finds out for real.
    async function isIndexedDBAvailable(idb) {
        const api = idb
            || (typeof globalThis !== 'undefined' ? globalThis.indexedDB : undefined)
            || (typeof self !== 'undefined' ? self.indexedDB : undefined);
        if (!api || typeof api.open !== 'function') return false;
        const probe = '__omlx_capability_probe__';
        try {
            const db = await new Promise((resolve, reject) => {
                const request = api.open(probe, 1);
                request.onupgradeneeded = () => {
                    if (!request.result.objectStoreNames.contains('probe')) {
                        request.result.createObjectStore('probe');
                    }
                };
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(toError(request.error, 'IndexedDBProbeError'));
                request.onblocked = () => reject(new Error('probe blocked'));
            });
            db.close();
            try { api.deleteDatabase(probe); } catch {}
            return true;
        } catch {
            try { api.deleteDatabase(probe); } catch {}
            return false;
        }
    }

    // Origin-wide quota, as an enhancement. Reports `available: false` — never
    // throws — when the browser withholds navigator.storage, which is the normal
    // state over plain HTTP. Chat saving does not depend on this.
    async function browserQuota() {
        const sm = storageManager();
        if (!sm) {
            return { ok: true, available: false, quota: null, usage: null, persisted: null };
        }
        try {
            const est = await sm.estimate();
            let persisted = null;
            if (typeof sm.persisted === 'function') {
                try { persisted = await sm.persisted(); } catch {}
            }
            return {
                ok: true,
                available: true,
                quota: est?.quota ?? null,
                usage: est?.usage ?? null,
                persisted,
            };
        } catch (error) {
            return { ok: false, available: false, quota: null, usage: null, persisted: null, error };
        }
    }

    root.createIndexedDBRecordStore = createIndexedDBRecordStore;
    root.isIndexedDBAvailable = isIndexedDBAvailable;
    root.browserStorageQuota = browserQuota;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            createIndexedDBRecordStore,
            isIndexedDBAvailable,
            browserStorageQuota: browserQuota,
            DB_NAME,
            DB_VERSION,
        };
    }
})(typeof globalThis !== 'undefined' ? globalThis : window);
