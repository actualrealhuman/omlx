// SPDX-License-Identifier: Apache-2.0

// Per-chat record store for the web chat.
//
// Phase 2 of the storage redesign (docs/decisions/0002-chat-storage-redesign.md).
// Replaces the single whole-history value with one record per chat plus a small
// metadata index, so appending a message costs one record write instead of
// re-serializing the entire corpus.
//
// The interface is asynchronous even though the localStorage backend is
// synchronous underneath. Call sites convert to `await` here, in the phase that
// carries no backend risk, so the IndexedDB swap in phase 3 does not have to
// touch them again.
//
// Safety semantics from docs/decisions/0001 are preserved per record: a failed
// write never discards the previously committed bytes, malformed stored data is
// reported as recoverable rather than as an empty record, and nothing is ever
// deleted to make room.

(function (root) {
    'use strict';

    const RECORD_PREFIX = 'omlx_chat_v2:';
    const INDEX_KEY = 'omlx_chat_v2_index';
    const META_KEY = 'omlx_chat_v2_meta';
    const SCHEMA_VERSION = 2;

    function isQuotaError(error) {
        return error?.name === 'QuotaExceededError'
            || error?.name === 'NS_ERROR_DOM_QUOTA_REACHED'
            || error?.code === 22
            || error?.code === 1014
            || /quota/i.test(String(error?.message || ''));
    }

    function classifyWriteError(error) {
        return isQuotaError(error) ? 'quota' : 'unavailable';
    }

    function byteLength(text) {
        if (typeof text !== 'string') return 0;
        if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(text).length;
        return text.length;
    }

    // Reads a key without throwing. Storage access itself can fail (private
    // mode, disabled storage), and that is a distinct failure from the value
    // being unreadable.
    function readRaw(storage, key) {
        try {
            return { ok: true, raw: storage.getItem(key) };
        } catch (error) {
            return { ok: false, kind: 'unavailable', error };
        }
    }

    // Parses a record. A malformed value is reported as recoverable data with
    // its raw bytes attached; it is never treated as an absent record, because
    // an absent record may be overwritten and a corrupt one may not.
    function parseRecord(raw) {
        if (raw == null) return { ok: true, record: null };
        let parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (error) {
            return { ok: false, kind: 'corrupt', raw, error };
        }
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            return {
                ok: false,
                kind: 'corrupt',
                raw,
                error: new TypeError('Chat record is not an object'),
            };
        }
        return { ok: true, record: parsed };
    }

    // This backend has no blobs store, so inline base64 cannot be kept: a couple of
    // images would exhaust the localStorage quota and block saving the conversation
    // outright. The bytes are blanked here — in the backend that cannot hold them —
    // rather than in each caller, and the count is reported so the UI can say so
    // instead of dropping attachments without a word. The IndexedDB backend keeps
    // them; see chat_media_store.js.
    function dropInlineMedia(messages) {
        let stripped = 0;
        const list = Array.isArray(messages) ? messages : [];
        const out = list.map((msg) => {
            if (!msg || typeof msg !== 'object' || !Array.isArray(msg.content)) return msg;
            const content = msg.content.map((part) => {
                if (!part || typeof part !== 'object') return part;
                if (part.type === 'image_url' && /^data:/i.test(String(part.image_url?.url || ''))) {
                    stripped += 1;
                    return { ...part, image_url: { url: '', mediaDropped: 'backend' } };
                }
                if (part.type === 'file' && part.file?.data) {
                    stripped += 1;
                    return {
                        ...part,
                        file: {
                            filename: part.file.filename || '',
                            mime_type: part.file.mime_type || '',
                            data: '',
                            mediaDropped: 'backend',
                        },
                    };
                }
                return part;
            });
            return { ...msg, content };
        });
        return { messages: out, stripped };
    }

    function createLocalStorageRecordStore(options = {}) {
        const storage = options.storage;
        if (!storage) throw new TypeError('createLocalStorageRecordStore requires a storage');

        const prefix = options.prefix || RECORD_PREFIX;
        const indexKey = options.indexKey || INDEX_KEY;
        const metaKey = options.metaKey || META_KEY;

        const recordKey = (id) => `${prefix}${id}`;

        function readIndex() {
            const read = readRaw(storage, indexKey);
            if (!read.ok) return read;
            if (read.raw == null) return { ok: true, index: {} };
            try {
                const parsed = JSON.parse(read.raw);
                if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                    return { ok: false, kind: 'corrupt', raw: read.raw, error: new TypeError('Chat index is not an object') };
                }
                return { ok: true, index: parsed };
            } catch (error) {
                return { ok: false, kind: 'corrupt', raw: read.raw, error };
            }
        }

        function writeIndex(index) {
            let serialized;
            try {
                serialized = JSON.stringify(index);
            } catch (error) {
                return { ok: false, kind: 'serialization', error };
            }
            try {
                storage.setItem(indexKey, serialized);
                return { ok: true };
            } catch (error) {
                return { ok: false, kind: classifyWriteError(error), error };
            }
        }

        function readMeta() {
            const read = readRaw(storage, metaKey);
            if (!read.ok) return { ok: false, kind: read.kind, error: read.error };
            if (read.raw == null) return { ok: true, meta: null };
            try {
                return { ok: true, meta: JSON.parse(read.raw) };
            } catch (error) {
                // A corrupt meta value means we cannot trust the store has been
                // migrated. Report it; the caller decides.
                return { ok: false, kind: 'corrupt', error };
            }
        }

        function writeMeta(meta) {
            try {
                storage.setItem(metaKey, JSON.stringify(meta));
                return { ok: true };
            } catch (error) {
                return { ok: false, kind: classifyWriteError(error), error };
            }
        }

        // ---- interface (async surface, synchronous implementation) ----

        async function get(id) {
            const read = readRaw(storage, recordKey(id));
            if (!read.ok) return read;
            const parsed = parseRecord(read.raw);
            if (!parsed.ok) return parsed;
            return { ok: true, record: parsed.record, raw: read.raw };
        }

        // put() enforces optimistic concurrency. When `expectRev` is supplied and
        // the stored record's rev differs, the write is refused and the current
        // record is returned so the caller can surface the conflict.
        //
        // localStorage gives no atomic read-modify-write, so this is best-effort
        // here and becomes transactional in the IndexedDB backend.
        //
        // `preserveRev` is for migration only: it carries a source record's
        // revision forward so a stale tab still collides. Ordinary writes omit it.
        async function put(record, { expectRev = null, preserveRev = false } = {}) {
            if (!record || typeof record !== 'object' || record.id == null) {
                return { ok: false, kind: 'invalid', error: new TypeError('Record requires an id') };
            }

            const key = recordKey(record.id);
            const before = readRaw(storage, key);
            if (!before.ok) return before;

            const beforeParsed = parseRecord(before.raw);
            if (!beforeParsed.ok) {
                // Refuse to overwrite unreadable data. The caller must resolve the
                // corruption explicitly, exactly as in the 0001 design.
                return { ok: false, kind: 'corrupt', raw: before.raw, error: beforeParsed.error };
            }

            const existing = beforeParsed.record;
            const currentRev = Number.isInteger(existing?.rev) ? existing.rev : 0;

            if (expectRev !== null && expectRev !== currentRev) {
                return {
                    ok: false,
                    kind: 'conflict',
                    expectedRev: expectRev,
                    currentRev,
                    current: existing,
                    raw: before.raw,
                };
            }

            const carried = preserveRev && Number.isInteger(record.rev)
                ? Math.max(record.rev, currentRev)
                : currentRev + 1;
            const nextRev = Math.max(carried, currentRev);
            const offloaded = dropInlineMedia(record.messages);
            let serialized;
            try {
                serialized = JSON.stringify({ ...record, messages: offloaded.messages, rev: nextRev });
            } catch (error) {
                return { ok: false, kind: 'serialization', error, raw: before.ok ? before.raw : null };
            }

            try {
                storage.setItem(key, serialized);
            } catch (error) {
                return {
                    ok: false,
                    kind: classifyWriteError(error),
                    error,
                    // The previously committed bytes are still what a subsequent
                    // read will return; hand them back so the UI can offer them.
                    raw: before.ok ? before.raw : null,
                };
            }

            const indexed = {
                id: record.id,
                title: record.title ?? '',
                model: record.model ?? null,
                createdAt: record.createdAt ?? null,
                updatedAt: record.updatedAt ?? null,
                rev: nextRev,
                bytes: byteLength(serialized),
                messageCount: Array.isArray(record.messages) ? record.messages.length : 0,
            };

            const idx = readIndex();
            if (!idx.ok) {
                // The record committed; only the index is broken. Report it as a
                // degraded index rather than a failed write, so the caller does not
                // discard a record that is safely on disk.
                return { ok: true, rev: nextRev, mediaStripped: offloaded.stripped, indexDegraded: true, indexIssue: idx };
            }
            const nextIndex = { ...idx.index, [record.id]: indexed };
            const written = writeIndex(nextIndex);
            if (!written.ok) {
                return { ok: true, rev: nextRev, mediaStripped: offloaded.stripped, indexDegraded: true, indexIssue: written };
            }

            return { ok: true, rev: nextRev, mediaStripped: offloaded.stripped };
        }

        async function remove(id) {
            const key = recordKey(id);
            const before = readRaw(storage, key);
            try {
                storage.removeItem(key);
            } catch (error) {
                return { ok: false, kind: classifyWriteError(error), error, raw: before.ok ? before.raw : null };
            }
            const idx = readIndex();
            if (idx.ok) {
                const nextIndex = { ...idx.index };
                delete nextIndex[id];
                writeIndex(nextIndex);
            }
            return { ok: true, removedRaw: before.ok ? before.raw : null };
        }

        async function listIndex() {
            const idx = readIndex();
            if (!idx.ok) return idx;
            return { ok: true, entries: Object.values(idx.index) };
        }

        async function getMeta() {
            return readMeta();
        }

        async function setMeta(patch) {
            const current = readMeta();
            const base = current.ok && current.meta ? current.meta : {};
            return writeMeta({ ...base, ...patch, schemaVersion: SCHEMA_VERSION });
        }

        // Enumerate record ids from keys, for callers that need the truth rather
        // than the index (recovery, GC, index rebuild).
        async function keys() {
            const ids = [];
            const enumerate = storage.keys || storage.key;
            if (typeof storage.keys === 'function') {
                for (const k of await storage.keys()) {
                    if (typeof k === 'string' && k.startsWith(prefix)) ids.push(k.slice(prefix.length));
                }
            } else if (typeof storage.length === 'number' && typeof storage.key === 'function') {
                for (let i = 0; i < storage.length; i += 1) {
                    const k = storage.key(i);
                    if (typeof k === 'string' && k.startsWith(prefix)) ids.push(k.slice(prefix.length));
                }
            }
            return { ok: true, ids };
        }

        // Rebuild the index from the records themselves. Used when the index is
        // corrupt or missing but the records are intact.
        async function rebuildIndex() {
            const listed = await keys();
            if (!listed.ok) return listed;
            const nextIndex = {};
            const corrupt = [];
            for (const id of listed.ids) {
                const got = await get(id);
                if (!got.ok) {
                    if (got.kind === 'corrupt') corrupt.push({ id, raw: got.raw });
                    continue;
                }
                if (!got.record) continue;
                const serialized = got.raw ?? '';
                nextIndex[id] = {
                    id,
                    title: got.record.title ?? '',
                    model: got.record.model ?? null,
                    createdAt: got.record.createdAt ?? null,
                    updatedAt: got.record.updatedAt ?? null,
                    rev: Number.isInteger(got.record.rev) ? got.record.rev : 0,
                    bytes: byteLength(serialized),
                    messageCount: Array.isArray(got.record.messages) ? got.record.messages.length : 0,
                };
            }
            const written = writeIndex(nextIndex);
            if (!written.ok) return written;
            return { ok: true, count: Object.keys(nextIndex).length, corrupt };
        }

        // Removes every record and the index. Never invoked to reclaim space;
        // only from an explicit user action.
        async function clearAll() {
            const listed = await keys();
            if (!listed.ok) return listed;
            const removed = [];
            for (const id of listed.ids) {
                const before = readRaw(storage, recordKey(id));
                try {
                    storage.removeItem(recordKey(id));
                    if (before.ok && before.raw != null) removed.push(before.raw);
                } catch (error) {
                    return { ok: false, kind: classifyWriteError(error), error, removed };
                }
            }
            try {
                storage.removeItem(indexKey);
            } catch (error) {
                return { ok: false, kind: classifyWriteError(error), error, removed };
            }
            return { ok: true, removed };
        }

        async function totalBytes() {
            const idx = readIndex();
            if (!idx.ok) return idx;
            let total = 0;
            for (const entry of Object.values(idx.index)) total += Number(entry.bytes) || 0;
            return { ok: true, bytes: total };
        }

        return {
            backend: 'localStorage',
            schemaVersion: SCHEMA_VERSION,
            supportsBlobs: false,
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
            recordKey,
        };
    }

    root.createLocalStorageRecordStore = createLocalStorageRecordStore;
    root.dropInlineMedia = dropInlineMedia;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            createLocalStorageRecordStore,
            isQuotaError,
            parseRecord,
            byteLength,
            dropInlineMedia,
            RECORD_PREFIX,
            INDEX_KEY,
            META_KEY,
            SCHEMA_VERSION,
        };
    }
})(typeof globalThis !== 'undefined' ? globalThis : window);
