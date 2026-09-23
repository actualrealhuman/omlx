// Run with: node --test tests/chat_history_migration.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
    createLocalStorageRecordStore,
    RECORD_PREFIX,
} = require('../omlx/admin/static/js/chat_record_store.js');
const {
    migrateLegacyHistory,
    migrationState,
    retireLegacyBackup,
    inspectLegacy,
    partition,
    LEGACY_HISTORY_KEY,
    MIGRATION_META_KEY,
} = require('../omlx/admin/static/js/chat_history_migration.js');

class MemoryStorage {
    constructor(initial = {}) {
        this.map = new Map(Object.entries(initial));
        this.writeError = null;
        this.readError = null;
        this.removeCalls = 0;
    }
    getItem(key) {
        if (this.readError) throw this.readError;
        return this.map.has(key) ? this.map.get(key) : null;
    }
    setItem(key, value) {
        if (this.writeError) throw this.writeError;
        this.map.set(key, String(value));
    }
    removeItem(key) {
        this.removeCalls += 1;
        if (this.writeError) throw this.writeError;
        this.map.delete(key);
    }
    key(i) { return Array.from(this.map.keys())[i] ?? null; }
    get length() { return this.map.size; }
    quotaError() {
        const e = new Error('QuotaExceeded');
        e.name = 'QuotaExceededError';
        return e;
    }
}

const legacyChat = (id, overrides = {}) => ({
    id,
    title: `Old ${id}`,
    model: 'legacy-model',
    createdAt: 100,
    updatedAt: 200,
    messages: [{ role: 'user', content: `hi ${id}` }],
    ...overrides,
});

function setup(history, initial = {}) {
    const legacyStorage = new MemoryStorage({
        [LEGACY_HISTORY_KEY]: history == null ? undefined : JSON.stringify(history),
        ...initial,
    });
    const storage = legacyStorage;
    const recordStore = createLocalStorageRecordStore({ storage });
    return { storage, legacyStorage, recordStore };
}

test('migrates every legacy chat into its own record', async () => {
    const { recordStore, storage } = setup([legacyChat('a'), legacyChat('b'), legacyChat('c')]);
    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.ok, true);
    assert.equal(result.migrated, 3);
    assert.equal(result.skipped, 0);
    assert.deepEqual(result.rejected, []);

    const a = await recordStore.get('a');
    assert.equal(a.ok, true);
    assert.equal(a.record.title, 'Old a');
    assert.equal(a.record.rev, 1);

    const listed = await recordStore.listIndex();
    assert.deepEqual(listed.entries.map(e => e.id).sort(), ['a', 'b', 'c']);
});

test('the legacy value is retained, never removed, by migration', async () => {
    const history = [legacyChat('a')];
    const { storage, recordStore } = setup(history);
    const raw = storage.getItem(LEGACY_HISTORY_KEY);

    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.retained, true);
    assert.equal(storage.getItem(LEGACY_HISTORY_KEY), raw);
    assert.equal(storage.removeCalls, 0);
});

test('the backend marker is written only after verification', async () => {
    const { storage, recordStore } = setup([legacyChat('a'), legacyChat('b')]);
    await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    const state = await migrationState(recordStore);
    assert.equal(state.migrated, true);
    assert.equal(state.info.migrated, 2);
    assert.equal(state.info.legacyRetained, true);
});

test('an absent legacy value is a clean no-op', async () => {
    const { storage, recordStore } = setup(null);
    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.ok, true);
    assert.equal(result.empty, true);
    assert.equal(result.migrated, 0);
    assert.equal((await migrationState(recordStore)).migrated, false);
});

test('corrupt legacy JSON is reported and nothing is written', async () => {
    const storage = new MemoryStorage({ [LEGACY_HISTORY_KEY]: '{definitely broken' });
    const recordStore = createLocalStorageRecordStore({ storage });

    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.ok, false);
    assert.equal(result.kind, 'corrupt');
    assert.equal(result.migrated, 0);
    assert.equal(result.retained, true);
    assert.equal(storage.getItem(LEGACY_HISTORY_KEY), '{definitely broken');
    assert.equal((await recordStore.listIndex()).entries.length, 0);
    assert.equal((await migrationState(recordStore)).migrated, false);
});

test('a non-array legacy value is corrupt, not empty', async () => {
    const storage = new MemoryStorage({ [LEGACY_HISTORY_KEY]: JSON.stringify({ sneaky: true }) });
    const recordStore = createLocalStorageRecordStore({ storage });
    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });
    assert.equal(result.ok, false);
    assert.equal(result.kind, 'corrupt');
});

test('unreadable legacy storage is unavailable, not empty', async () => {
    const storage = new MemoryStorage();
    storage.readError = new Error('SecurityError');
    const recordStore = createLocalStorageRecordStore({ storage });
    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });
    assert.equal(result.ok, false);
    assert.equal(result.kind, 'unavailable');
});

test('entries without an id are rejected and reported, not dropped silently', async () => {
    const { storage, recordStore } = setup([
        legacyChat('a'),
        { title: 'no id here' },
        null,
        legacyChat('b'),
    ]);

    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.ok, true);
    assert.equal(result.migrated, 2);
    assert.equal(result.rejected.length, 2);
    assert.deepEqual(result.rejected.map(r => r.reason), ['missing-id', 'missing-id']);
    assert.deepEqual((await recordStore.listIndex()).entries.map(e => e.id).sort(), ['a', 'b']);
});

test('duplicate ids are rejected, keeping the first occurrence', async () => {
    const { storage, recordStore } = setup([
        legacyChat('a', { title: 'first' }),
        legacyChat('a', { title: 'second' }),
    ]);

    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.migrated, 1);
    assert.deepEqual(result.rejected.map(r => r.reason), ['duplicate-id']);
    assert.equal((await recordStore.get('a')).record.title, 'first');
});

test('migration is idempotent: a second run skips everything', async () => {
    const { storage, recordStore } = setup([legacyChat('a'), legacyChat('b')]);

    const first = await migrateLegacyHistory({ legacyStorage: storage, recordStore });
    const second = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(first.migrated, 2);
    assert.equal(second.migrated, 0);
    assert.equal(second.skipped, 2);
    assert.equal(second.ok, true);
    assert.equal((await recordStore.listIndex()).entries.length, 2);
});

test('a newer existing record is never overwritten by migration', async () => {
    const { storage, recordStore } = setup([legacyChat('a', { title: 'OLD' })]);
    await recordStore.put({ id: 'a', title: 'NEWER', messages: [], updatedAt: 999 });

    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.skipped, 1);
    assert.equal((await recordStore.get('a')).record.title, 'NEWER');
});

test('a quota failure mid-migration leaves the marker unwritten and legacy intact', async () => {
    const history = [legacyChat('a'), legacyChat('b'), legacyChat('c')];
    const { storage, recordStore } = setup(history);

    let writes = 0;
    const originalSet = storage.setItem.bind(storage);
    storage.setItem = (key, value) => {
        writes += 1;
        if (key === `${RECORD_PREFIX}c`) throw storage.quotaError();
        return originalSet(key, value);
    };

    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.ok, false);
    assert.equal(result.kind, 'quota');
    assert.equal(result.migrated, 2);
    assert.equal(result.retained, true);
    assert.equal(storage.getItem(LEGACY_HISTORY_KEY), JSON.stringify(history));
    assert.equal((await migrationState(recordStore)).migrated, false);

    // The two records that did commit are still readable.
    storage.setItem = originalSet;
    assert.equal((await recordStore.get('a')).ok, true);
    assert.equal((await recordStore.get('b')).ok, true);
});

test('a corrupt record with a legacy id blocks that entry but not the run', async () => {
    const { storage, recordStore } = setup([legacyChat('a'), legacyChat('b')]);
    storage.setItem(`${RECORD_PREFIX}b`, '}{corrupt');

    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.ok, false);
    assert.equal(result.migrated, 1);
    assert.equal(result.failures[0].kind, 'corrupt');
    assert.equal(storage.getItem(`${RECORD_PREFIX}b`), '}{corrupt');
    assert.equal((await migrationState(recordStore)).migrated, false);
});

test('migrationState reports false before any migration', async () => {
    const { recordStore } = setup([legacyChat('a')]);
    const state = await migrationState(recordStore);
    assert.equal(state.migrated, false);
    assert.equal(state.info, null);
});

test('retireLegacyBackup removes the backup and reports the bytes freed', async () => {
    const history = [legacyChat('a'), legacyChat('b')];
    const { storage, recordStore } = setup(history);
    await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    const retired = retireLegacyBackup(storage);
    assert.equal(retired.ok, true);
    assert.ok(retired.removedBytes > 0);
    assert.equal(storage.getItem(LEGACY_HISTORY_KEY), null);

    // Records survive retiring the legacy backup.
    assert.equal((await recordStore.get('a')).ok, true);
});

test('retireLegacyBackup on an absent value is a harmless no-op', () => {
    const storage = new MemoryStorage();
    const retired = retireLegacyBackup(storage);
    assert.equal(retired.ok, true);
    assert.equal(retired.removedBytes, 0);
});

test('inspectLegacy separates valid, corrupt, and absent', () => {
    const storage = new MemoryStorage();
    assert.equal(inspectLegacy(storage).empty, true);
    storage.setItem(LEGACY_HISTORY_KEY, 'nope');
    assert.equal(inspectLegacy(storage).kind, 'corrupt');
    storage.setItem(LEGACY_HISTORY_KEY, '[]');
    assert.equal(inspectLegacy(storage).ok, true);
});

test('partition keeps order and reports the original index', () => {
    const { valid, rejected } = partition([legacyChat('a'), 42, legacyChat('b')]);
    assert.deepEqual(valid.map(v => v.id), ['a', 'b']);
    assert.deepEqual(rejected.map(r => r.index), [1]);
});

test('a large legacy corpus migrates without a count cap', async () => {
    const history = Array.from({ length: 1200 }, (_, i) => legacyChat(`chat_${i}`));
    const { storage, recordStore } = setup(history);

    const result = await migrateLegacyHistory({ legacyStorage: storage, recordStore });

    assert.equal(result.ok, true);
    assert.equal(result.migrated, 1200);
    assert.equal((await recordStore.listIndex()).entries.length, 1200);
});

test('progress callback reports as records commit', async () => {
    const { storage, recordStore } = setup([legacyChat('a'), legacyChat('b'), legacyChat('c')]);
    const seen = [];
    await migrateLegacyHistory({
        legacyStorage: storage,
        recordStore,
        onProgress: (p) => seen.push(p.migrated),
    });
    assert.deepEqual(seen, [1, 2, 3]);
});

test('migration records the source key in meta', async () => {
    const { storage, recordStore } = setup([legacyChat('a')]);
    await migrateLegacyHistory({ legacyStorage: storage, recordStore });
    const meta = (await recordStore.getMeta()).meta;
    assert.equal(meta[MIGRATION_META_KEY].sourceKey, LEGACY_HISTORY_KEY);
    assert.equal(meta.backend, 'localStorage');
});
