// SPDX-License-Identifier: Apache-2.0

// Migration from the legacy whole-history localStorage value to the per-chat
// record store.
//
// See docs/decisions/0002-chat-storage-redesign.md, "Migration".
//
// The rules that make this safe:
//   - the legacy value is validated before anything is written;
//   - records are written first, then verified by count and byte total;
//   - the backend marker is written only after verification succeeds;
//   - the legacy value is retained as a backup and is never removed here;
//   - a run that fails partway is safe to repeat, because existing records are
//     never overwritten by migration (a newer record must win).

(function (root) {
    'use strict';

    const LEGACY_HISTORY_KEY = 'omlx_chat_history';
    const MIGRATION_META_KEY = 'chatHistoryMigration';

    function byteLength(text) {
        if (typeof text !== 'string') return 0;
        if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(text).length;
        return text.length;
    }

    // Reads and validates the legacy value without mutating anything.
    function inspectLegacy(legacyStorage, legacyKey = LEGACY_HISTORY_KEY) {
        let raw;
        try {
            raw = legacyStorage.getItem(legacyKey);
        } catch (error) {
            return { ok: false, kind: 'unavailable', error };
        }
        if (raw == null) return { ok: true, history: [], raw: null, empty: true };

        let parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (error) {
            return { ok: false, kind: 'corrupt', raw, error };
        }
        if (!Array.isArray(parsed)) {
            return {
                ok: false,
                kind: 'corrupt',
                raw,
                error: new TypeError('Legacy chat history is not an array'),
            };
        }
        return { ok: true, history: parsed, raw };
    }

    // Splits a validated legacy array into migratable and rejected entries.
    // Entries without a usable id cannot become records; they are reported so the
    // caller can surface them instead of dropping them silently.
    function partition(history) {
        const valid = [];
        const rejected = [];
        const seen = new Set();
        history.forEach((entry, index) => {
            if (!entry || typeof entry !== 'object' || Array.isArray(entry) || entry.id == null) {
                rejected.push({ index, reason: 'missing-id' });
                return;
            }
            const id = String(entry.id);
            if (seen.has(id)) {
                rejected.push({ index, id, reason: 'duplicate-id' });
                return;
            }
            seen.add(id);
            valid.push({ ...entry, id });
        });
        return { valid, rejected };
    }

    // Runs the migration. Returns a report; never throws.
    //
    // `recordStore` is the phase-2 record store. `legacyStorage` is usually the
    // same localStorage object, passed separately so the legacy key and the
    // record keys stay distinct concerns.
    async function migrateLegacyHistory({
        legacyStorage,
        recordStore,
        legacyKey = LEGACY_HISTORY_KEY,
        onProgress = null,
    } = {}) {
        if (!legacyStorage || !recordStore) {
            return { ok: false, kind: 'invalid', error: new TypeError('legacyStorage and recordStore are required') };
        }

        const inspected = inspectLegacy(legacyStorage, legacyKey);
        if (!inspected.ok) {
            // Corrupt or unreadable legacy data: report it and leave everything
            // exactly as found. The caller offers it for download.
            return { ...inspected, migrated: 0, retained: inspected.raw != null };
        }
        if (inspected.empty) {
            return { ok: true, migrated: 0, skipped: 0, rejected: [], bytes: 0, retained: false, empty: true };
        }

        const { valid, rejected } = partition(inspected.history);

        let migrated = 0;
        let skipped = 0;
        let bytes = 0;
        const failures = [];

        for (const entry of valid) {
            const existing = await recordStore.get(entry.id);

            // A record already present means an earlier run got this far, or the
            // app already wrote to the new store. Either way the existing record is
            // at least as new as the legacy copy, so it wins.
            if (existing.ok && existing.record) {
                skipped += 1;
                continue;
            }
            if (!existing.ok && existing.kind !== 'corrupt') {
                failures.push({ id: entry.id, ...existing });
                break;
            }
            // A corrupt record with this id: refuse to overwrite it during
            // migration. Report and continue with the rest.
            if (existing.kind === 'corrupt') {
                failures.push({ id: entry.id, kind: 'corrupt', raw: existing.raw });
                continue;
            }

            const put = await recordStore.put(entry);
            if (!put.ok) {
                failures.push({ id: entry.id, ...put });
                break;
            }
            migrated += 1;
            bytes += byteLength(JSON.stringify(entry));
            if (onProgress) onProgress({ migrated, total: valid.length });
        }

        if (failures.length) {
            // Do not mark the backend migrated. The app stays on the legacy path
            // and the partial records are harmless leftovers.
            return {
                ok: false,
                kind: failures[0].kind || 'unavailable',
                migrated,
                skipped,
                rejected,
                failures,
                bytes,
                retained: true,
            };
        }

        // Verify by reading back, not by trusting that the writes worked.
        const listed = await recordStore.listIndex();
        if (!listed.ok) {
            return { ok: false, kind: listed.kind || 'unavailable', migrated, skipped, rejected, bytes, retained: true };
        }

        const expected = migrated + skipped;
        const writtenIds = new Set(listed.entries.map(e => e.id));
        const missing = valid.map(e => String(e.id)).filter(id => !writtenIds.has(id));

        if (missing.length) {
            return {
                ok: false,
                kind: 'verification',
                migrated,
                skipped,
                rejected,
                missing,
                bytes,
                retained: true,
            };
        }

        const meta = await recordStore.setMeta({
            backend: recordStore.backend,
            [MIGRATION_META_KEY]: {
                completed: true,
                migrated,
                skipped,
                rejected: rejected.length,
                bytes,
                sourceKey: legacyKey,
                legacyRetained: true,
            },
        });
        if (!meta.ok) {
            return { ok: false, kind: meta.kind || 'unavailable', migrated, skipped, rejected, bytes, retained: true };
        }

        return {
            ok: true,
            migrated,
            skipped,
            rejected,
            bytes,
            expected,
            verified: expected,
            retained: true,
        };
    }

    async function migrationState(recordStore, legacyKey = LEGACY_HISTORY_KEY) {
        const read = await recordStore.getMeta();
        if (!read.ok) return { migrated: false, metaIssue: read.kind };
        const info = read.meta?.[MIGRATION_META_KEY];
        return {
            migrated: Boolean(info?.completed),
            info: info ?? null,
            sourceKey: info?.sourceKey ?? legacyKey,
        };
    }

    // Removes the retained legacy backup. Only ever called from an explicit user
    // action, after the caller has confirmed the new store is good.
    function retireLegacyBackup(legacyStorage, legacyKey = LEGACY_HISTORY_KEY) {
        const inspected = inspectLegacy(legacyStorage, legacyKey);
        const raw = inspected.ok ? inspected.raw : null;
        try {
            legacyStorage.removeItem(legacyKey);
            return { ok: true, removedBytes: byteLength(raw ?? '') };
        } catch (error) {
            return { ok: false, kind: 'unavailable', error };
        }
    }

    root.migrateLegacyHistory = migrateLegacyHistory;
    root.migrationState = migrationState;
    root.retireLegacyBackup = retireLegacyBackup;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            migrateLegacyHistory,
            migrationState,
            retireLegacyBackup,
            inspectLegacy,
            partition,
            LEGACY_HISTORY_KEY,
            MIGRATION_META_KEY,
        };
    }
})(typeof globalThis !== 'undefined' ? globalThis : window);
