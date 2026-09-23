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

    async function cursorAll(store, mapper) {
        const out = [];
        let cursor = await reqToPromise(store.openCursor());
        while (cursor) {
            out.push(mapper ? mapper(cursor.value) : cursor.value);
            cursor = await reqToPromise(cursor.continue());
        }
        return out;
    }

    function byteLengthOf(value) {
        try {
            return typeof value === 'string' ? value.length : JSON.stringify(value).length;
        } catch {
            return 0;
        }
    }

    function createIndexedDBRecordStore(options = {}) {
        const idb = options.indexedDB;
        if (!idb) throw new TypeError('createIndexedDBRecordStore requires indexedDB');

        const name = options.name || DB_NAME;
        const version = options.version || DB_VERSION;
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
                            // Populated in phase 4; declared now so the schema does not
                            // need a version bump to add media later.
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
            const quota = error?.name === 'QuotaExceededError'
                || error?.name === 'NS_ERROR_DOM_QUOTA_REACHED'
                || /quota/i.test(String(error?.message || ''));
            return quota ? 'quota' : 'unavailable';
        }

        // ---- interface ----

        async function get(id) {
            let db;
            try { db = await open(); } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
            try {
                const record = await reqToPromise(db.transaction(CHATS, 'readonly').objectStore(CHATS).get(id));
                return { ok: true, record: record ?? null };
            } catch (error) {
                return { ok: false, kind: 'unavailable', error };
            }
        }

        // Atomic compare-and-set. The read of the current revision and the write
        // happen in the same transaction, so two tabs cannot both believe they won.
        //
        // `preserveRev` exists for migration only: it carries the source record's
        // revision forward so a tab still holding the old backend's revision
        // collides here instead of silently overwriting. Ordinary writes never use
        // it — they let the store mint the next revision.
        async function put(record, { expectRev = null, preserveRev = false } = {}) {
            if (!record || typeof record !== 'object' || record.id == null) {
                return { ok: false, kind: 'invalid', error: new TypeError('Record requires an id') };
            }
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
            const index = tx.objectStore(CHAT_INDEX);

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

                const carried = preserveRev && Number.isInteger(record.rev)
                    ? Math.max(record.rev, currentRev)
                    : currentRev + 1;
                const nextRev = Math.max(carried, currentRev);
                const stored = { ...record, rev: nextRev };
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
                return { ok: true, rev: nextRev };
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
                store.put({ key: 'app', value: { ...base, ...patch, schemaVersion: DB_VERSION } });
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
            storeNames: { CHATS, CHAT_INDEX, META, BLOBS },
        };
    }

    // Cheap capability probe. Private browsing and some configurations reject
    // IndexedDB outright, and the caller must fall back rather than assume.
    async function isIndexedDBAvailable(indexedDB) {
        if (!indexedDB || typeof indexedDB.open !== 'function') return false;
        const probe = '__omlx_capability_probe__';
        try {
            const db = await new Promise((resolve, reject) => {
                const request = indexedDB.open(probe, 1);
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
            try { indexedDB.deleteDatabase(probe); } catch {}
            return true;
        } catch {
            try { indexedDB.deleteDatabase(probe); } catch {}
            return false;
        }
    }

    root.createIndexedDBRecordStore = createIndexedDBRecordStore;
    root.isIndexedDBAvailable = isIndexedDBAvailable;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            createIndexedDBRecordStore,
            isIndexedDBAvailable,
            DB_NAME,
            DB_VERSION,
        };
    }
})(typeof globalThis !== 'undefined' ? globalThis : window);
